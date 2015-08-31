# Functions to install files on all nodes.

import os
import json
import logging

from BroControl import util
from BroControl import config


# In all paths given in this file, ${<option>} will replaced with the value of
# the corresponding configuration option.

class Port:
    def __init__(self, startport):
        self.p = startport

    def next_port(self, node):
        self.p += 1
        node.setPort(self.p)
        return self.p

# Directories/files in form (path, mirror) which are synced from the manager to
# all remote hosts.
# If "mirror" is False, then the path is assumed to be a directory and it will
# just be created on the remote host.  If "mirror" is True, then the path
# is fully mirrored recursively.
def get_syncs():
    syncs = [
    ("${brobase}", True),
    ("${brobase}/share", True),
    ("${cfgdir}", True),
    ("${libdir}", True),
    ("${bindir}", True),
    ("${policydirsiteinstall}", True),
    ("${policydirsiteinstallauto}", True),
    # ("${policydir}", True),
    # ("${staticdir}", True),
    ("${logdir}", False),
    ("${spooldir}", False),
    #("${spooldir}/broctl-config.sh", False),
    ("${broctlconfigdir}/broctl-config.sh", True),
    ("${tmpdir}", False)
    ]

    return syncs

# In NFS-mode, only these will be synced.
def get_nfssyncs():
    nfssyncs = [
    ("${spooldir}", False),
    ("${tmpdir}", False),
    ("${policydirsiteinstall}", True),
    ("${policydirsiteinstallauto}", True),
    ("${broctlconfigdir}/broctl-config.sh", True)
    ]

    return nfssyncs

# Generate shell script that sets Broctl dynamic variables according
# to their current values.  This shell script gets included in all
# other scripts.
def make_broctl_config_sh(cmdout):
    cfg_path = os.path.join(config.Config.broctlconfigdir, "broctl-config.sh")

    with open(cfg_path, "w") as out:
        for (varname, value) in config.Config.options():
            # Don't write if variable name is an invalid bash variable name
            if "-" not in varname:
                value = str(value)
                # Don't write if the value contains any double quotes (this
                # could happen for BroArgs, which we don't need in this file)
                if '"' not in value:
                    out.write("%s=\"%s\"\n" % (varname.replace(".", "_"), value))

    symlink = os.path.join(config.Config.scriptsdir, "broctl-config.sh")

    try:
        if os.readlink(symlink) != cfg_path:
            # attempt to update the symlink
            try:
                util.force_symlink(cfg_path, symlink)
            except OSError as e:
                cmdout.error("failed to update symlink '%s' to point to '%s': %s" % (symlink, cfg_path, e.strerror))
                return False
    except OSError as e:
        cmdout.error("failed to resolve symlink '%s': %s" % (symlink, e.strerror))
        return False

    return True

# Create Bro-side broctl configuration file.
def make_layout(path, cmdout, silent=False):

    if config.Config.nodes("standalone"):
        make_standalone_layout(path, cmdout, silent)
    else:
        make_cluster_layout(path, cmdout, silent)

def make_standalone_layout(path, cmdout, silent):
    manager = config.Config.manager()
    logging.debug("make_standalone_layout, manager is " + str(manager.name))
    broport = Port(int(config.Config.broport) - 1)

    # If there is a standalone node, delete any cluster-layout file to
    # avoid the cluster framework from activating and get out of here.
    filename = os.path.join(path, "cluster-layout.bro")
    if os.access(filename, os.W_OK):
        os.unlink(filename)

    # We do need to establish the port for the manager.
    if not silent:
        cmdout.info("generating standalone-layout.bro ...")

    filename = os.path.join(path, "standalone-layout.bro")
    with open(filename, "w") as out:
        out.write("# Automatically generated. Do not edit.\n")
        # This is the port that standalone nodes listen on for remote
        # control by default.
        out.write("redef Communication::listen_port = %s/tcp;\n" % broport.next_port(manager))
        out.write("redef Communication::nodes += {\n")
        out.write("\t[\"control\"] = [$host=%s, $zone_id=\"%s\", $class=\"control\"],\n" % (util.format_bro_addr(manager.addr), manager.zone_id))
        out.write("};\n")

def get_cluster_roles(rlist):
    roles = ""
    for r in rlist:
        if roles != "":
            roles += ", "
        if r == "manager":
            roles += "Cluster::MANAGER"
        elif r == "datanode":
            roles += "Cluster::DATANODE"
        elif r == "lognode":
            roles += "Cluster::LOGNODE"
        elif r == "worker":
            roles += "Cluster::WORKER"
        else:
            raise RuntimeError("node role not found")

    return roles

def make_cluster_layout(path, cmdout, silent=False):
    manager=config.Config.manager()

    logging.debug("make_cluster_layout, manager is " + str(manager.name))
    broport = Port(int(config.Config.broport) - 1)

    if not silent:
        cmdout.info("generating cluster-layout.bro ...")

    workers = config.Config.nodes("workers")
    datanodes = config.Config.nodes("datanodes")

    out = ""
    out += "# Automatically generated. Do not edit.\n"
    out += "redef Cluster::nodes = {\n"
    out += "\t[\"control\"] = [$node_roles=set(Cluster::CONTROL), $ip=%s, $zone_id=\"%s\", $p=%s/tcp],\n" % (util.format_bro_addr(manager.addr), config.Config.zoneid, broport.next_port(manager))

    for n in config.Config.nodes():
        # Manager definition
        if n == manager:
            out += "\t[\"%s\"] = [$node_roles=set(%s), $ip=%s, $zone_id=\"%s\", $p=%s/tcp, $workers=set(" % (manager.name, get_cluster_roles(manager.roles), util.format_bro_addr(manager.addr), manager.zone_id, broport.next_port(manager))
            for s in workers:
                out += "\"%s\"" % s.name
                if s != workers[-1]:
                    out += ", "
            out += ")],\n"

        # Datanode definition
        elif "datanode" in n.roles or "lognode" in n.roles:
            out += "\t[\"%s\"] = [$node_roles=set(%s), $ip=%s, $zone_id=\"%s\", $p=%s/tcp, $manager=\"%s\", $workers=set(" % (n.name, get_cluster_roles(n.roles), util.format_bro_addr(n.addr), n.zone_id, broport.next_port(n), manager.name)
            for s in workers:
                out += "\"%s\"" % s.name
                if s != workers[-1]:
                    out += ", "
            out += ")],\n"

        # Workers definition
        elif "worker" in n.roles:
            p = len(workers) % len(datanodes)
            out += "\t[\"%s\"] = [$node_roles=set(%s), $ip=%s, $zone_id=\"%s\", $p=%s/tcp, $interface=\"%s\", $manager=\"%s\", $datanode=\"%s\"],\n" % (n.name, get_cluster_roles(n.roles), util.format_bro_addr(n.addr), n.zone_id, broport.next_port(n), n.interface, manager.name, datanodes[p].name)

    # Activate time-machine support if configured.
    if config.Config.timemachinehost:
        out += "\t[\"time-machine\"] = [$node_roles=set(Cluster::TIME_MACHINE), $ip=%s, $p=%s],\n" % (config.Config.timemachinehost, config.Config.timemachineport)

    out += "};\n"

    filename = os.path.join(path, "cluster-layout.bro")
    file_out = open(filename, "w")
    file_out.write(out)
    file_out.close()

    logging.debug("out:" + str(out))

# Reads in a list of networks from file.
def read_networks(fname):

    nets = []

    with open(fname, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            fields = line.split(None, 1)

            cidr = util.format_bro_prefix(fields[0])
            tag = ""
            if len(fields) == 2:
                tag = fields[1]

            nets += [(cidr, tag)]

    return nets


# Create Bro script which contains a list of local networks.
def make_local_networks(path, cmdout, silent=False):

    netcfg = config.Config.localnetscfg

    if not os.path.exists(netcfg):
        cmdout.error("file not found: %s" % netcfg)
        return False

    nets = read_networks(netcfg)

    with open(os.path.join(path, "local-networks.bro"), "w") as out:
        out.write("# Automatically generated. Do not edit.\n\n")

        out.write("redef Site::local_nets = {\n")
        for (cidr, tag) in nets:
            out.write("\t%s," % cidr)
            if tag:
                out.write("\t# %s" % tag)
            out.write("\n")
        out.write("};\n\n")

    return True


def make_broctl_config_policy(path, cmdout, silent=False):
    manager = config.Config.manager()

    filename = os.path.join(path, "broctl-config.bro")
    with open(filename, "w") as out:
        out.write("# Automatically generated. Do not edit.\n")
        out.write("redef Notice::mail_dest = \"%s\";\n" % config.Config.mailto)
        out.write("redef Notice::mail_dest_pretty_printed = \"%s\";\n" % config.Config.mailalarmsto)
        out.write("redef Notice::sendmail  = \"%s\";\n" % config.Config.sendmail)
        out.write("redef Notice::mail_subject_prefix  = \"%s\";\n" % config.Config.mailsubjectprefix)
        out.write("redef Notice::mail_from  = \"%s\";\n" % config.Config.mailfrom)
        if "standalone" not in manager.roles:
            out.write("@if ( Cluster::has_local_role(Cluster::MANAGER) )\n")
        out.write("redef Log::default_rotation_interval = %s secs;\n" % config.Config.logrotationinterval)
        out.write("redef Log::default_mail_alarms_interval = %s secs;\n" % config.Config.mailalarmsinterval)
        if "standalone" not in manager.roles:
            out.write("@endif\n")

        if config.Config.ipv6comm == "1":
            out.write("redef Communication::listen_ipv6 = T ;\n")
        else:
            out.write("redef Communication::listen_ipv6 = F ;\n")


# Write individual node.cfg file per successor in the hierarchy
def makeNodeConfigs(path, peers, cmdout, silent=False):
    if not silent:
        cmdout.info("generating node.cfg per peer ...")

    localNode = config.Config.get_local()
    logging.debug(str(config.Config.get_local_id()) + "@" + str(config.Config.localaddrs[0]) + " :: create node configuration")

    for p in peers:
        if p != localNode:
            makeNodeConfig(path, p, cmdout, silent)


# Write individual node.cfg file for node
def makeNodeConfig(path, node, cmdout, silent=False):
    overlay = config.Config.overlay
    localNode = config.Config.get_local()

    npath = os.path.join (path, "node.json_" + str(node.name))
    logging.debug(str(localNode.name) + " write node.json for " + str(node.name) + " to path " + str(npath))

    g = ""
    if hasattr(node, "cluster"):
        g = overlay.getSubgraph(node.cluster)
    else:
        g= overlay.getSubgraph(node.name)

    edgeNum = len(g.edges())
    nodeNum = len(g.nodes())

    with open(npath, 'w') as f:
        f.write("{\n")

        # 1. head entry
        f.write("\"head\" : {\n")
        f.write("\"id\": \"" + str(localNode.name) + "\",\n")
        if hasattr(localNode, "roles"):
            f.write("\"roles\": \"" + str(localNode.roles) + "\",\n")
        if hasattr(localNode, "cluster"):
            f.write("\"cluster\": \"" + str(localNode.cluster) + "\",\n")
        if hasattr(localNode, "addr"):
            f.write("\"host\": \"" + str(localNode.addr) + "\",\n")
        if hasattr(localNode, "port"):
            f.write("\"port\": \"" + str(localNode.port) + "\"\n")
        f.write("},\n\n")

        # 2. node entries
        counter = 0
        f.write("\"nodes\" : [\n")
        for n2 in g.nodes():
            counter += 1
            if nodeNum - counter == 0:
                f.write(json.dumps(overlay.node_attr["json-data"][n2]) + "\n")
            else:
                f.write(json.dumps(overlay.node_attr["json-data"][n2]) + ",\n")
        f.write("],\n\n")

        # 3. connection entries
        f.write("\"connections\" : [\n")

        counter = 0
        for (u, v) in g.edges():
            counter += 1
            if edgeNum - counter == 0:
                f.write("{\"from\": \"" + str(u) + "\", \"to\": \"" + str(v) + "\"}\n")
            else:
                f.write("{\"from\": \"" + str(u) + "\", \"to\": \"" + str(v) + "\"},\n")
        f.write("]\n")

        f.write("}\n")
