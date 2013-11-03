import confluent.messages as msg

def retrieve(nodes, element, configmanager, inputdata):
    attributes = configmanager.get_node_attributes(nodes)
    for node in attributes.iterkeys():
        for attribute in attributes[node].iterkeys():
            currattr = attributes[node][attribute]
            if 'value' in currattr:
                yield msg.Attributes(node,
                    {attribute: currattr['value']})
            elif 'cryptvalue' in currattr:
                yield msg.CryptedAttributes(node,
                    {attribute: currattr['cryptvalue']})
            else:
                print repr(currattr)
                raise Exception("BUGGY ATTRIBUTE FOR NODE")


def update(nodes, element, configmanager, inputdata):
    updatedict = {}
    for node in nodes:
        updatenode = inputdata.get_attributes(node)
        if updatenode:
            updatedict[node] = updatenode
    configmanager.set_node_attributes(updatedict)
