# Functions to read and access the broctl configuration.

import os
import socket
import re
import json
import logging
import shutil

from BroControl import py3bro
from BroControl import node as node_mod
from BroControl import options
from .state import SqliteState
from .version import VERSION

from BroControl import graph

# TODO move in options.py
USE_BROKER = False

# Class storing the broctl configuration.
#
# This class provides access to four types of configuration/state:
#
# - the global broctl configuration from broctl.cfg
# - the node configuration from node.cfg
# - dynamic state variables which are kept across restarts in spool/state.db

Config = None  # Globally accessible instance of Configuration.

class ConfigurationError(Exception):
    pass

class Configuration:
    def __init__(self, basedir, ui, localaddrs=[], state=None):
        from BroControl import execute

        config_file = os.path.join(basedir, "etc/broctl.cfg")
        broscriptdir = os.path.join(basedir, "share/bro")

        self.ui = ui
        self.localaddrs = localaddrs
        logging.debug("localaddrs:", localaddrs)

        global Config
        Config = self

        self.config = {}
        self.state = {}
        self.nodestore = {}

        # Read broctl.cfg.
        self.config = self._read_config(config_file)

        # Set defaults for options we get passed in.
        self._set_option("brobase", basedir)
        self._set_option("broscriptdir", broscriptdir)
        self._set_option("version", VERSION)

        # Initialize options.
        for opt in options.options:
            if not opt.dontinit:
                self._set_option(opt.name, opt.default)

        if state:
            self.state_store = state
        else:
            self.state_store = SqliteState(self.statefile)

        # Set defaults for options we derive dynamically.
        self._set_option("mailto", "%s" % os.getenv("USER"))
        self._set_option("mailfrom", "Big Brother <bro@%s>" % socket.gethostname())
        self._set_option("mailalarmsto", self.config["mailto"])

        # One directory per node.cfg per peer
        # TODO hostname should be replaced by unique identifier per node/peer
        nodecfgpath = os.path.join(self.cfgdir, str(socket.gethostname()))
        if not os.path.exists(nodecfgpath):
            os.makedirs(nodecfgpath)
            shutil.move(self.nodecfg, nodecfgpath)
        self.nodecfg = os.path.join(nodecfgpath, "node.cfg")

        # Determine operating system.
        (success, output) = execute.run_localcmd("uname")
        if not success:
            raise RuntimeError("cannot run uname")
        self._set_option("os", output[0].lower().strip())

        if self.config["os"] == "linux":
            self._set_option("pin_command", "taskset -c")
        elif self.config["os"] == "freebsd":
            self._set_option("pin_command", "cpuset -l")
        else:
            self._set_option("pin_command", "")

        # Find the time command (should be a GNU time for best results).
        (success, output) = execute.run_localcmd("which time")
        if success:
            self._set_option("time", output[0].lower().strip())
        else:
            self._set_option("time", "")

        # Hierarchy overlay
        self.overlay = graph.BGraph()

    def initPostPlugins(self):
        self.read_state()

        # Read node.cfg
        self.nodestore, self.local_node, self.head = self._read_nodes()
        if not self.nodestore:
            return False

        # If "env_vars" was specified in broctl.cfg, then apply to all nodes.
        varlist = self.config.get("env_vars")
        if varlist:
            try:
                global_env_vars = self._get_env_var_dict(varlist)
            except ConfigurationError as err:
                raise ConfigurationError("env_vars option in broctl.cfg: %s" % err)

            for node in self.nodes("all"):
                for (key, val) in global_env_vars.items():
                    # Values from node.cfg take precedence over broctl.cfg
                    node.env_vars.setdefault(key, val)

        # Set the standalone config option.
        standalone = "0"
        for node in self.nodes("all"):
            if node.type == "standalone":
                standalone = "1"

        self._set_option("standalone", standalone)

        # Make sure cron flag is cleared.
        self.config["cron"] = "0"

    # Provides access to the configuration options via the dereference operator.
    # Lookup the attribute in broctl options first, then in the dynamic state
    # variables.
    def __getattr__(self, attr):
        if attr in self.config:
            return self.config[attr]
        if attr in self.state:
            return self.state[attr]
        raise AttributeError(attr)

    # Returns True if attribute is defined.
    def has_attr(self, attr):
        if attr in self.config:
            return True
        if attr in self.state:
            return True
        return False

    # Returns a sorted list of all broctl.cfg entries.
    # Includes dynamic variables if dynamic is true.
    def options(self, dynamic=True):
        optlist = list(self.config.items())
        if dynamic:
            optlist += list(self.state.items())

        optlist.sort()
        return optlist

    # Returns a list of Nodes (the list will be empty if no matching nodes
    # are found).  The returned list is sorted by node type, and by node name
    # for each type.
    # - If tag is None, all Nodes are returned.
    # - If tag is "all", all Nodes are returned if "expand_all" is true.
    #     If "expand_all" is false, returns an empty list in this case.
    # - If tag is "proxies", all proxy Nodes are returned.
    # - If tag is "workers", all worker Nodes are returned.
    # - If tag is "manager", the manager Node is returned (cluster config) or
    #     the standalone Node is returned (standalone config).
    # - If tag is "standalone", the standalone Node is returned.
    # - If tag is the name of a node, then that node is returned.
    def nodes(self, tag=None, expand_all=True):
        nodes = []
        nodetype = None

        if tag == "all" or tag == "cluster":
            if not expand_all:
                return []

        elif tag == "standalone":
            nodetype = "standalone"

        elif tag == "manager":
            nodetype = "manager"

        elif tag == "proxies":
            nodetype = "proxy"

        elif tag == "workers":
            nodetype = "worker"

        elif tag == "peers":
            nodetype = "peer"

        for n in self.nodestore.values():
            if nodetype == n.type:
                if nodetype == "peer" and n.type == "peer" and self.get_local() == n:
                    continue
                nodes += [n]

            elif tag == n.name:
                nodes += [n]

            elif tag == "cluster" and (n.type != "peer"):
                nodes += [n]

            elif tag == "all" or not tag:
                nodes += [n]

        nodes.sort(key=lambda n: (n.type, n.name))

        if not nodes and tag == "manager":
            nodes = self.nodes("standalone")

        return nodes

    def manager(self):
        n = self.nodes("manager")
        if n:
            return n[0]

        n = self.nodes("standalone")
        if n:
            return n[0]

        return None

    # Returns a list of nodes which is a subset of the result a similar call to
    # nodes() would yield but within which each host appears only once.
    # If "nolocal" parameter is True, then exclude the local host from results.
    def hosts(self, tag=None, nolocal=False):
        hosts = {}
        nodelist = []

        for node in self.nodes(tag):
            if node.host in hosts:
                continue
            if (not nolocal) or (nolocal and node.addr not in self.localaddrs):
                hosts[node.host] = 1
                nodelist.append(node)

        return nodelist

    # Returns a Node entry for the local node
    def get_local_id(self):
        if self.local_node:
            return self.local_node.name
        else:
            return "unknown"

    # Returns a Node entry for the local node
    def get_local(self):
        return self.local_node

    # Returns a Node entry for our predecessor in the hierarchy
    def get_head(self):
        return self.head

    # Replace all occurences of "${option}", with option being either
    # broctl.cfg option or a dynamic variable, with the corresponding value.
    # Defaults to replacement with the empty string for unknown options.
    def subst(self, text):
        while True:
            match = re.search(r"(\$\{([A-Za-z]+)(:([^}]+))?\})", text)
            if not match:
                return text

            key = match.group(2).lower()
            if self.has_attr(key):
                value = self.__getattr__(key)
            else:
                value = match.group(4)

            if not value:
                value = ""

            text = text[0:match.start(1)] + value + text[match.end(1):]


    # Convert string into list of integers (ValueError is raised if any
    # item in the list is not a non-negative integer).
    def _get_pin_cpu_list(self, text, numprocs):
        if not text:
            return []

        cpulist = [int(x) for x in text.split(",")]
        # Minimum allowed CPU number is zero.
        if min(cpulist) < 0:
            raise ValueError

        # Make sure list is at least as long as number of worker processes.
        cpulen = len(cpulist)
        if numprocs > cpulen:
            cpulist = [ cpulist[i % cpulen] for i in range(numprocs) ]

        return cpulist

    # Convert a string consisting of a comma-separated list of environment
    # variables (e.g. "VAR1=123, VAR2=456") to a dictionary.
    # If the string is empty, then return an empty dictionary.
    def _get_env_var_dict(self, text):
        env_vars = {}

        if text:
            # If the entire string is quoted, then remove only those quotes.
            if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
                text = text[1:-1]

        if text:
            for keyval in text.split(","):
                try:
                    (key, val) = keyval.split("=", 1)
                except ValueError:
                    raise ConfigurationError("missing '=' in env_vars")

                if not key.strip():
                    raise ConfigurationError("missing environment variable name in env_vars")

                env_vars[key.strip()] = val.strip()

        return env_vars


    # Parse node.cfg.
    def _read_nodes(self):
        config = py3bro.configparser.SafeConfigParser()
        fname = self.nodecfg
        try:
            if not config.read(fname):
                raise ConfigurationError("cannot read '%s'" % fname)
            self.json = False
        except py3bro.configparser.MissingSectionHeaderError:
            self.json = True
            return self._read_nodes_json()

        nodestore = {}

        counts = {}
        for sec in config.sections():
            node = node_mod.Node(self, sec)

            for (key, val) in config.items(sec):

                key = key.replace(".", "_")

                if key not in node_mod.Node._keys:
                    self.ui.warn("ignoring unrecognized node config option '%s' given for node '%s'" % (key, sec))
                    continue

                node.__dict__[key] = val

            self._check_node(node, nodestore, counts)

            if node.name in nodestore:
                raise ConfigurationError("Duplicate node name '%s'" % node.name)
            nodestore[node.name] = node

        self._check_nodestore(nodestore)

        return nodestore, node_mod.Node(self, "unknown"), None

    def _check_node(self, node, nodestore, counts):
        if not node.type:
            raise ConfigurationError("No type given for node %s" % node.name)

        if node.type not in ("manager", "proxy", "worker", "standalone", "peer"):
            raise ConfigurationError("Unknown node type '%s' given for node '%s'" % (node.type, node.name))

        if not node.host:
            raise ConfigurationError("No host given for node '%s'" % node.name)

        try:
            addrinfo = socket.getaddrinfo(node.host, None, 0, 0, socket.SOL_TCP)
        except socket.gaierror as e:
            raise ConfigurationError("Unknown host '%s' given for node '%s' [%s]" % (node.host, node.name, e.args[1]))

        addr_str = addrinfo[0][4][0]
        # zone_id is handled manually, so strip it if it's there
        node.addr = addr_str.split("%")[0]

        # Convert env_vars from a string to a dictionary.
        try:
            node.env_vars = self._get_env_var_dict(node.env_vars)
        except ConfigurationError as err:
            raise ConfigurationError("Node '%s' config: %s" % (node.name, err))

        # Each node gets a number unique across its type.
        try:
            counts[node.type] += 1
        except KeyError:
            counts[node.type] = 1

        node.count = counts[node.type]

        numprocs = 0

        if node.lb_procs:
            if node.type != "worker":
                raise ConfigurationError("Load balancing node config options are only for worker nodes")
            try:
                numprocs = int(node.lb_procs)
            except ValueError:
                raise ConfigurationError("Number of load-balanced processes must be an integer for node '%s'" % node.name)
            if numprocs < 2:
                raise ConfigurationError("Number of load-balanced processes must be at least 2 for node '%s'" % node.name)
        elif node.lb_method:
            raise ConfigurationError("Number of load-balanced processes not specified for node '%s'" % node.name)

        try:
            pin_cpus = self._get_pin_cpu_list(node.pin_cpus, numprocs)
        except ValueError:
            raise ConfigurationError("Pin cpus list must contain only non-negative integers for node '%s'" % node.name)

        if pin_cpus:
            node.pin_cpus = pin_cpus[0]

        if node.lb_procs:
            if not node.lb_method:
                raise ConfigurationError("No load balancing method given for node '%s'" % node.name)

            if node.lb_method not in ("pf_ring", "myricom", "interfaces"):
                raise ConfigurationError("Unknown load balancing method '%s' given for node '%s'" % (node.lb_method, node.name))

            if node.lb_method == "interfaces":
                if not node.lb_interfaces:
                    raise ConfigurationError("List of load-balanced interfaces not specified for node '%s'" % node.name)

                # get list of interfaces to use, and assign one to each node
                netifs = node.lb_interfaces.split(",")

                if len(netifs) != numprocs:
                    raise ConfigurationError("Number of load-balanced interfaces is not same as number of load-balanced processes for node '%s'" % node.name)

                node.interface = netifs.pop().strip()

            origname = node.name
            # node names will have a numerical suffix
            node.name = "%s-1" % node.name

            for num in range(2, numprocs + 1):
                newnode = node.copy()
                # only the node name, count, and pin_cpus need to be changed
                newname = "%s-%d" % (origname, num)
                newnode.name = newname
                if newname in nodestore:
                    raise ConfigurationError("Duplicate node name '%s'" % newname)
                nodestore[newname] = newnode
                counts[node.type] += 1
                newnode.count = counts[node.type]
                if pin_cpus:
                    newnode.pin_cpus = pin_cpus[num-1]

                if newnode.lb_method == "interfaces":
                    newnode.interface = netifs.pop().strip()

    def _check_nodestore(self, nodestore):
        if not nodestore:
            raise ConfigurationError("No nodes found")

        standalone = False
        manager = False
        proxy = False
        peer = False

        manageronlocalhost = False

        for n in nodestore.values():
            if n.type == "manager":
                if manager:
                    raise ConfigurationError("Only one manager can be defined")
                manager = True

 #               if n.addr in ("127.0.0.1", "::1"):
 #                   manageronlocalhost = True

                if n.addr not in self.localaddrs:
                    raise ConfigurationError("Must run broctl only on manager node")

            elif n.type == "proxy":
                proxy = True

            elif n.type == "standalone":
                standalone = True

            elif n.type == "peer":
                peer = True

        if standalone:
            if len(nodestore) > 1 and not peer:
                raise ConfigurationError("More than one node defined in standalone node config")
        else:
            if not manager:
                raise ConfigurationError("No manager defined in node config")
            elif not proxy:
                raise ConfigurationError("No proxy defined in node config")

        # If manager is on localhost, then all other nodes must be on localhost
        #if manageronlocalhost:
        #    for n in nodestore.values():
        #        if n.type != "manager" and n.addr not in ("127.0.0.1", "::1"):
        #            raise ConfigurationError("cannot use localhost/127.0.0.1/::1 for manager host in nodes configuration")

    # Parse node.cfg in Json-Format
    def _read_nodes_json(self):
        file = self.nodecfg
        logging.debug(str(self.localaddrs[0]) + " :: read the node.cfg configuration from file " + str(file))
        if not os.path.exists(file):
            raise ConfigurationError("cannot read '%s'" % self.nodecfg)

        nodestore = {}
        counts = {}
        # the predecessor in the hierarchy / the local node in case it is the
        # root of the complete hierarchy
        head = None

        plainData = None
        with open(file, 'r') as f:
            plainData = f.readlines()

        with open(file, 'r') as f:
            try:
                data = json.load(f)
            except ValueError:
                logging.debug(str(self.localaddrs[0]) + " :: json data to read: " + str(plainData))
                raise ConfigurationError(str(self.localaddrs[0]) + " :: Json: node.cfg could not be decoded")

            if "nodes" not in data.keys() or "connections" not in data.keys() or "head" not in data.keys():
                raise ConfigurationError("Misconfigured node.cfg. One entry out of [head, node, connections] missing.")

            #
            # 1. Iterate over all node entries and create node objects for them
            #
            for entry in data["nodes"]:
                if "id" not in entry.keys() or "type" not in entry.keys():
                    raise ConfigurationError("Misconfigured configuration file")

                node = ""
                # Node entry is a cluster
                if entry["type"] == "cluster":
                    clusterId = entry["id"]
                    # Add cluster to the graph
                    self.overlay.addNodeAttr(clusterId, "json-data", entry)

                    for val in entry["members"]:
                        if "id" not in val.keys() or "type" not in val.keys():
                            raise ConfigurationError("Misconfigured configuration file")

                        nodeId = clusterId + "::" + val["id"]
                        #node, nodestore, counts = self._get_node_json(val, nodeId, nodestore, counts)
                        node = self._get_node_json(val, nodeId, nodestore, counts)
                        node.__dict__["cluster"] = clusterId

                # Node entry is ordinary node
                else:
                    nodeId= entry["id"]
                    if "::" in nodeId:
                        raise ConfigurationError("Misconfigured ID entry of a node, id should not contain \"::\"")
                    #node, nodestore, counts = self._get_node_json(entry, nodeId, nodestore, counts)
                    node = self._get_node_json(entry, nodeId, nodestore, counts)
                    # Add node to the graph
                    self.overlay.addNodeAttr(nodeId, "json-data", entry)

                if not node:
                    raise ConfigurationError("no node found in node.cfg")

            #
            # 2. Create a node entry for the head node of the local subtree, which
            #    is either the local node (when it is the root) or its predecessor
            #
            if "id" not in data["head"].keys():
                raise ConfigurationError("Misconfigured node.cfg. Head entry invalid")

            headId = ""
            if "cluster" in data["head"] and data["head"]["cluster"]:
                headId = data["head"]["cluster"] + "::" + data["head"]["id"]
            else:
                headId = data["head"]["id"]

            # we are not the head
            # (and thus the head entry was not included in the nodes section)
            if headId not in nodestore.keys():
                #head, nodestore, counts = self._get_node_json(data["head"], headId, nodestore, counts)
                head = self._get_node_json(data["head"], headId, nodestore, counts)

            else:  # we are the head
                head = nodestore[headId]

            if not hasattr(head, "host") and not hasattr(head, "type"):
                raise ConfigurationError("Misconfigured node.cfg. Head entry invalid")

            #
            # 3. Parse connections between nodes
            #
            if "connections" in data.keys():
                for val in data["connections"]:
                    self.overlay.addEdge(val["from"], val["to"])


        if not self.overlay.isConnected() or not self.overlay.isTree():
            raise ConfigurationError("Misconfigured overlay.")

        # Current scope of this peer:
        # 1. local node
        # 2. its cluster (proxy and worker nodes)
        # 3. its peers (predecessor and successors in hierarchy)
        scopelist = {}

        # root/head of the tree
        root = self.overlay.getRoot()
        logging.debug("_read_nodes_json: root is " + str(root))

        # The local node is the manager of its subtree
        if root in nodestore.keys():
            local_n = nodestore[root]
            if local_n.type == "peer":
                local_n.type = "standalone"
                nodestore[root] = local_n

        local_node = None
        # The direct successors of the root
        peer_list = self.overlay.getSuccessors(root)

        for key, node in nodestore.iteritems():
            if key == root:
                scopelist[key] = node
                local_node = node

            elif hasattr(node, "cluster") and node.cluster == root:
                scopelist[key] = node
                if node.type in ("manager", "standalone"):
                    local_node = node

            elif key in peer_list:  # and key != headId:
                node.type = "peer"
                scopelist[key] = node

            elif hasattr(node, "cluster") and node.cluster in peer_list and node.type == "manager":
                node.type = "peer"
                scopelist[node.cluster] = node

        if not local_node:
            raise RuntimeError("No node configuration for local node found")

        # Check if nodestore is valid
        self._check_nodestore(scopelist)

        logging.debug("local node is " + root + " and head is " + str(head))

        return scopelist, local_node, head

    def _get_node_json(self, val, nodeId, nodestore, counts):
        node = node_mod.Node(self, nodeId)
        node = self._extractNodeJson(val, node)
        nodestore[nodeId] = node
        #node, nodestore, counts = self._check_node(node, nodestore, counts)
        self._check_node(node, nodestore, counts)

        #return node, nodestore, counts
        return node

    # Parses a node entry in Json format
    def _extractNodeJson(self, entry, node):
        for key in entry:
            node.__dict__[key] = entry[key]

        if hasattr(node, "type") and node.type not in ["manager", "proxy", "worker", "standalone", "peer"]:
            raise ConfigurationError("strange node type detected, node " + node.name + " is " + str(node.type))

        return node

    # Parses broctl.cfg and returns a dictionary of all entries.
    def _read_config(self, fname):
        config = {}
        for line in open(fname):

            line = line.strip()
            if not line or line.startswith("#"):
                continue

            args = line.split("=", 1)
            if len(args) != 2:
                raise ConfigurationError("%s: syntax error '%s'" % (fname, line))

            (key, val) = args
            key = key.strip().lower()

            # if the key already exists, just overwrite with new value
            config[key] = val.strip()

        return config


    # Initialize a global option if not already set.
    def _set_option(self, key, val):
        key = key.lower()
        if key not in self.config:
            self.config[key] = self.subst(val)

    # Set a dynamic state variable.
    def set_state(self, key, val):
        key = key.lower()
        self.state[key] = val
        self.state_store.set(key, val)

    # Returns value of state variable, or None if it's not defined.
    def get_state(self, key):
        return self.state.get(key)

    # Read dynamic state variables.
    def read_state(self):
        self.state = dict(self.state_store.items())

    # Record the Bro version.
    def record_bro_version(self):
        try:
            version = self._get_bro_version()
        except ConfigurationError:
            return False

        self.set_state("broversion", version)
        self.set_state("bro", self.subst("${bindir}/bro"))
        return True


    # Warn user to run broctl install if any changes are detected to broctl
    # config options, node config, Bro version, or if certain state variables
    # are missing.
    def warn_broctl_install(self):
        missingstate = False

        # Check if Bro version is different from previously-installed version.
        if "broversion" in self.state:
            oldversion = self.state["broversion"]

            version = self._get_bro_version()

            if version != oldversion:
                self.ui.warn("new bro version detected (run the broctl \"restart --clean\" or \"install\" command)")
                return
        else:
            missingstate = True

        # Check if node config has changed since last install.
        if "hash-nodecfg" in self.state:
            nodehash = self._get_nodecfg_hash()

            if nodehash != self.state["hash-nodecfg"]:
                self.ui.warn("broctl node config has changed (run the broctl \"restart --clean\" or \"install\" command)")
                self._warn_dangling_bro()
                return
        else:
            missingstate = True

        # Check if any config values have changed since last install.
        if "hash-broctlcfg" in self.state:
            cfghash = self._get_broctlcfg_hash()
            if cfghash != self.state["hash-broctlcfg"]:
                self.ui.warn("broctl config has changed (run the broctl \"restart --clean\" or \"install\" command)")
                return
        else:
            missingstate = True

        # If any of the state variables don't exist, then we need to install
        # (this would most likely indicate an upgrade install was performed
        # over an old version that didn't have the state.db file).
        if missingstate:
            # Don't show warning if we've never run broctl install, because
            # nothing will work anyway without doing an initial install.
            if os.path.exists(os.path.join(self.config["scriptsdir"], "broctl-config.sh")):
                self.ui.warn("state database needs updating (run the broctl \"install\" command)")
            return

    # Warn if there might be any dangling Bro nodes (i.e., nodes that are
    # no longer part of the current node configuration, but that are still
    # running).
    def _warn_dangling_bro(self):
        nodes = [ n.name for n in self.nodes() ]

        for key in self.state.keys():
            # Check if a PID is defined for a Bro node
            if key.endswith("-pid") and self.get_state(key):
                nn = key[:-4]
                # Check if node name is in list of all known nodes
                if nn not in nodes:
                    hostkey = key.replace("-pid", "-host")
                    hname = self.get_state(hostkey)
                    if hname:
                        self.ui.warn("Bro node \"%s\" possibly still running on host \"%s\" (PID %s)" % (nn, hname, self.get_state(key)))

    # Return a hash value (as a string) of the current broctl configuration.
    def _get_broctlcfg_hash(self):
        return str(hash(tuple(sorted(self.config.items()))))

    # Update the stored hash value of the current broctl configuration.
    def update_broctlcfg_hash(self):
        cfghash = self._get_broctlcfg_hash()
        self.set_state("hash-broctlcfg", cfghash)

    # Return a hash value (as a string) of the current broctl node config.
    def _get_nodecfg_hash(self):
        nn = []
        for n in self.nodes():
            nn.append(tuple([(key, val) for key, val in n.items() if not key.startswith("_")]))
        return str(hash(tuple(nn)))

    # Update the stored hash value of the current broctl node config.
    def update_nodecfg_hash(self):
        nodehash = self._get_nodecfg_hash()
        self.set_state("hash-nodecfg", nodehash)

    # Runs Bro to get its version number.
    def _get_bro_version(self):
        from BroControl import execute

        version = ""
        bro = self.subst("${bindir}/bro")
        if os.path.lexists(bro):
            (success, output) = execute.run_localcmd("%s -v" % bro)
            if success and output:
                version = output[-1]
        else:
            raise ConfigurationError("cannot find Bro binary to determine version")

        match = re.search(".* version ([^ ]*).*$", version)
        if not match:
            raise ConfigurationError("cannot determine Bro version [%s]" % version.strip())

        version = match.group(1)
        # If bro is built with the "--enable-debug" configure option, then it
        # appends "-debug" to the version string.
        if version.endswith("-debug"):
            version = version[:-6]

        return version

    def writeJson(self, peer, data):
        print "write Json configuration file for peer " + str(peer)

    def use_broker(self):
        return USE_BROKER
