'''
Created on Aug 14, 2014

@author: preethi
'''
import pydot
import yaml
import sys
import os
import json
import logging
from jinja2 import Environment, PackageLoader
from model import Pod, Device, Interface, InterfaceLogical, InterfaceDefinition
from dao import Dao
import util

junosTemplateLocation = os.path.join('conf', 'junosTemplates')
cablingPlanTemplateLocation = os.path.join('conf', 'cablingPlanTemplates')

moduleName = 'writer'
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(moduleName)

class WriterBase():
    def __init__(self, conf, pod, dao):
        # get logger
        logging.basicConfig(level=logging.getLevelName(conf['logLevel'][moduleName]))
        logger = logging.getLogger(moduleName)
        
        # use dao to generate various output
        self.dao = dao
        
        # this writer is specific for this pod
        self.pod = pod
        
        self.conf = conf
        
        # resolve output directory
        if 'outputDir' in conf:
            outputPath = conf['outputDir']
            if (outputPath[-1] != '/'):
                outputPath += '/'
            self.outputDir = outputPath + pod.name
        else:
            self.outputDir = 'out/' + pod.name
        if not os.path.exists(self.outputDir):
            os.makedirs(self.outputDir)

class ConfigWriter(WriterBase):
    def __init__(self, conf, pod, dao):
        WriterBase.__init__(self, conf, pod, dao)
        self.templateEnv = Environment(loader=PackageLoader('jnpr.openclos', junosTemplateLocation))
    
    def write(self):
        for device in self.pod.devices:
            config = self.createBaseConfig(device)
            config += self.createInterfaces(device)
            config += self.createRoutingOption(device)
            config += self.createProtocols(device)
            config += self.createPolicyOption(device)
            config += self.createVlan(device)
            
            logger.info('Writing config for device: %s' % (device.name))
            with open(self.outputDir + "/" + device.name + '.conf', 'w') as f:
                    f.write(config)
            
    def createBaseConfig(self, device):
        with open(os.path.join(junosTemplateLocation, 'baseTemplate.txt'), 'r') as f:
            baseTemplate = f.read()
            f.close()
            return baseTemplate

    def createInterfaces(self, device): 
        with open(os.path.join(junosTemplateLocation, 'interface_stanza.txt'), 'r') as f:
            interfaceStanza = f.read()
            f.close()
        
        with open(os.path.join(junosTemplateLocation, 'lo0_stanza.txt'), 'r') as f:
            lo0Stanza = f.read()
            f.close()
            
        with open(os.path.join(junosTemplateLocation, 'mgmt_interface.txt'), 'r') as f:
            mgmtStanza = f.read()
            f.close()

        with open(os.path.join(junosTemplateLocation, 'rvi_stanza.txt'), 'r') as f:
            rviStanza = f.read()
            f.close()
            
        with open(os.path.join(junosTemplateLocation, 'server_interface_stanza.txt'), 'r') as f:
            serverInterfaceStanza = f.read()
            f.close()
            
        config = "interfaces {" + "\n" 
        # management interface
        candidate = mgmtStanza.replace("<<<mgmt_address>>>", device.managementIp)
        config += candidate
                
        #loopback interface
        loopbackIfl = self.dao.Session.query(InterfaceLogical).join(Device).filter(InterfaceLogical.name == 'lo0.0').filter(Device.id == device.id).one()
        candidate = lo0Stanza.replace("<<<address>>>", loopbackIfl.ipaddress)
        config += candidate

        # For Leaf add IRB and server facing interfaces        
        if device.role == 'leaf':
            irbIfl = self.dao.Session.query(InterfaceLogical).join(Device).filter(InterfaceLogical.name == 'irb.1').filter(Device.id == device.id).one()
            candidate = rviStanza.replace("<<<address>>>", irbIfl.ipaddress)
            config += candidate
            config += serverInterfaceStanza

        # Interconnect interfaces
        deviceInterconnectIfds = self.dao.Session.query(InterfaceDefinition).join(Device).filter(InterfaceDefinition.peer != None).filter(Device.id == device.id).order_by(InterfaceDefinition.name).all()
        for interconnectIfd in deviceInterconnectIfds:
            peerDevice = interconnectIfd.peer.device
            interconnectIfl = interconnectIfd.layerAboves[0]
            namePlusUnit = interconnectIfl.name.split('.')  # example et-0/0/0.0
            candidate = interfaceStanza.replace("<<<ifd_name>>>", namePlusUnit[0])
            candidate = candidate.replace("<<<unit>>>", namePlusUnit[1])
            candidate = candidate.replace("<<<description>>>", "facing_" + peerDevice.name)
            candidate = candidate.replace("<<<address>>>", interconnectIfl.ipaddress)
            config += candidate
                
        config += "}\n"
        return config

    def createRoutingOption(self, device):
        with open(os.path.join(junosTemplateLocation, 'routing_options_stanza.txt'), 'r') as f:
            routingOptionStanza = f.read()

        loopbackIfl = self.dao.Session.query(InterfaceLogical).join(Device).filter(InterfaceLogical.name == 'lo0.0').filter(Device.id == device.id).one()
        loopbackIpWithNoCidr = loopbackIfl.ipaddress.split('/')[0]
        
        candidate = routingOptionStanza.replace("<<<routerId>>>", loopbackIpWithNoCidr)
        candidate = candidate.replace("<<<asn>>>", str(device.asn))
        
        return candidate

    def createProtocols(self, device):
        template = self.templateEnv.get_template('protocolBgpLldp.txt')

        neighborList = []
        deviceInterconnectIfds = self.dao.Session.query(InterfaceDefinition).join(Device).filter(InterfaceDefinition.peer != None).filter(Device.id == device.id).order_by(InterfaceDefinition.name).all()
        for ifd in deviceInterconnectIfds:
            peerIfd = ifd.peer
            peerDevice = peerIfd.device
            peerInterconnectIfl = peerIfd.layerAboves[0]
            peerInterconnectIpNoCidr = peerInterconnectIfl.ipaddress.split('/')[0]
            neighborList.append({'peer_ip': peerInterconnectIpNoCidr, 'peer_asn': peerDevice.asn})

        return template.render(neighbors=neighborList)        
         
    def createPolicyOption(self, device):
        pod = device.pod
        
        template = self.templateEnv.get_template('policyOptions.txt')
        subnetDict = {}
        subnetDict['lo0_in'] = pod.allocatedLoopbackBlock
        subnetDict['irb_in'] = pod.allocatedIrbBlock
        
        if device.role == 'leaf':
            deviceLoopbackIfl = self.dao.Session.query(InterfaceLogical).join(Device).filter(InterfaceLogical.name == 'lo0.0').filter(Device.id == device.id).one()
            deviceIrbIfl = self.dao.Session.query(InterfaceLogical).join(Device).filter(InterfaceLogical.name == 'irb.1').filter(Device.id == device.id).one()
            subnetDict['lo0_out'] = deviceLoopbackIfl.ipaddress
            subnetDict['irb_out'] = deviceIrbIfl.ipaddress
        else:
            subnetDict['lo0_out'] = pod.allocatedLoopbackBlock
            subnetDict['irb_out'] = pod.allocatedIrbBlock
         
        return template.render(subnet=subnetDict)
        
    def createVlan(self, device):
        if device.role == 'leaf':
            template = self.templateEnv.get_template('vlans.txt')
            return template.render()
        else:
            return ''
                
class CablingPlanWriter(WriterBase):
    def __init__(self, conf, pod, dao):
        WriterBase.__init__(self, conf, pod, dao)
        self.templateEnv = Environment(loader=PackageLoader('jnpr.openclos', cablingPlanTemplateLocation))
        self.templateEnv.trim_blocks = True
        self.templateEnv.lstrip_blocks = True
        # load cabling plan template
        self.template = self.templateEnv.get_template(self.pod.topologyType + '.txt')
        # validity check
        if 'deviceFamily' not in self.conf:
            raise ValueError("No deviceFamily found in configuration file")

    def writeJSON(self):
        if self.pod.topologyType == 'threeStage':
            return self.writeJSONThreeStage()
        elif self.pod.topologyType == 'fiveStageRealEstate':
            return self.writeJSONFiveStageRealEstate()
        elif self.pod.topologyType == 'fiveStagePerformance':
            return self.writeJSONFiveStagePerformance()
            
    def writeJSONThreeStage(self):
        deviceDict = {}
        deviceDict['leaves'] = []
        deviceDict['spines'] = []
        for device in self.pod.devices:
            if (device.role == 'leaf'):
                deviceDict['leaves'].append(device.name)
            elif (device.role == 'spine'):
                deviceDict['spines'].append(device.name)
                
        spinePortNames = util.getPortNamesForDeviceFamily(self.pod.spineDeviceType, self.conf['deviceFamily'])
        leafPortNames = util.getPortNamesForDeviceFamily(self.pod.leafDeviceType, self.conf['deviceFamily'])
        
        # rendering cabling plan requires 4 parameters:
        # 1. list of spines
        # 2. list of spine ports (Note spine does not have any uplink/downlink marked, it is just ports)
        # 3. list of leaves
        # 4. list of leaf ports (Note leaf uses uplink to connect to spine)
        cablingPlanJSON = self.template.render(spines=deviceDict['spines'], 
                spinePorts=spinePortNames['ports'], 
                leaves=deviceDict['leaves'], 
                leafPorts=leafPortNames['uplinkPorts'])

        path = self.outputDir + '/cablingPlan.json'
        logger.info('Writing cabling plan: %s' % (path))
        with open(path, 'w') as f:
                f.write(cablingPlanJSON)

        # load cabling plan
        return json.loads(cablingPlanJSON)
               
    def writeJSONFiveStageRealEstate(self):
        pass
        
    def writeJSONFiveStagePerformance(self):
        pass
        
    def writeDOT(self):
        if self.pod.topologyType == 'threeStage':
            return self.writeDOTThreeStage()
        elif self.pod.topologyType == 'fiveStageRealEstate':
            return self.writeDOTFiveStageRealEstate()
        elif self.pod.topologyType == 'fiveStagePerformance':
            return self.writeDOTFiveStagePerformance()
    
    def writeDOTThreeStage(self):
        '''
        creates DOT file for devices in topology which has peers
        '''
       
        topology = self.createLabelForDevices(self.pod.devices, self.conf['DOT'])
        colors = self.conf['DOT']['colors']
        i =0
        for device in self.pod.devices:
            linkLabel = self.createLabelForLinks(device)
            if(i == len(colors)): 
                i=0
                self.createLinksInGraph(linkLabel, topology, colors[i])
                i+=1
            else:
                self.createLinksInGraph(linkLabel, topology, colors[i])
                i+=1
            
        path = self.outputDir + '/cablingPlan.dot'
        logger.info('Writing cabling plan: %s' % (path))
        topology.write_raw(path)

    def createLabelForDevices(self, devices, conf):
        #create the graph 
        ranksep = conf['ranksep']
        topology = pydot.Dot(graph_type='graph', splines='polyline', ranksep=ranksep)
        for device in devices:
            label = self.createLabelForDevice(device)
            self.createDeviceInGraph(label, device, topology)
        return topology    
                
    def createLabelForDevice(self, device):
        label = '{'
      
        label = label + '{'
        for ifd in device.interfaces: 
            if type(ifd) is InterfaceDefinition: 
                if ifd.role == 'uplink':
                    if ifd.peer is not None:
                        label += '<'+ifd.id+'>'+ ifd.name+'|'
                    
        if label.endswith('|'):
            label = label[:-1]
            label += '}|{' + device.name + '}|{'
        else:
            label += device.name + '}|{'
            
        for ifd in device.interfaces:
            if type(ifd) is InterfaceDefinition:
                if ifd.role == 'downlink':
                    if ifd.peer is not None:
                        label += '<'+ifd.id+'>'+ ifd.name+'|'
                    
        if label.endswith('|'):
            label = label[:-1]
            label += '}}'
        
            label = label[:-2]
            label += '}'
            
        return label

    def createDeviceInGraph(self, labelStrs, device, testDeviceLabel):
        #create device in DOT graph
        testDeviceLabel.add_node(pydot.Node(device.id, shape='record', label= labelStrs))
            
    def createLabelForLinks(self, device):
        links = {}
                      
        for ifd in device.interfaces:
            if type(ifd) is InterfaceDefinition:
                if ifd.role == 'downlink':
                    if ifd.peer is not None: 
                        interface =  '"'+ device.id +'"'+ ':' +'"'+ ifd.id +'"'
                        peer = '"'+ifd.peer.device.id +'"' + ':' +'"'+ ifd.peer.id +'"'
                        links[interface] = peer
                       
        return links

    def createLinksInGraph(self, links, linksInTopology, color):
        #create peer links between the devices in DOT graph
        for interface, peer in links.iteritems():
            linksInTopology.add_edge(pydot.Edge(interface, peer,color=color))

    def writeDOTFiveStageRealEstate(self):
        pass
        
    def writeDOTFiveStagePerformance(self):
        pass
        
