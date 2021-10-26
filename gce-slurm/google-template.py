# machine_type = 'n1-standard-1'
import string
import sys
import time


# SRC_IMAGE = 'https://www.googleapis.com/compute/v1/projects/debian-cloud/global/images/family/debian-10'
SRC_IMAGE = 'https://www.googleapis.com/compute/v1/projects/debian-cloud/global/images/family/debian-11'
SLURM_CONF = string.Template("""
ClusterName=bee-gce-slurm
SlurmctldHost=$slurmctld_host

MpiDefault=pmix
ProctrackType=proctrack/pgid
ReturnToService=2
SlurmctldPidFile=/var/run/slurmctld.pid
SlurmctldPort=7777
SlurmdPidFile=/var/run/slurmd.pid
SlurmdPort=8989
SlurmdSpoolDir=/var/spool/slurmd
SlurmUser=slurm

StateSaveLocation=/var/spool/slurmctld
SwitchType=switch/none

TaskPlugin=task/affinity

InactiveLimit=0
KillWait=30
MinJobAge=300
Waittime=0

SchedulerType=sched/backfill
SelectType=select/cons_tres
SelectTypeParameters=CR_Core

AccountingStorageType=accounting_storage/none
JobCompType=jobcomp/none
JobAcctGatherType=jobacct_gather/none
SlurmctldLogFile=/var/log/slurmctld.log
SlurmdDebug=info
SlurmdLogFile=/var/log/slurmd.log
AuthType=auth/munge

# NodeName=bee-main CPUs=1 Boards=1 SocketsPerBoard=1 CoresPerSocket=1 ThreadsPerCore=1 RealMemory=1982
# NodeName=bee-node0 CPUs=1 Boards=1 SocketsPerBoard=1 CoresPerSocket=1 ThreadsPerCore=1 RealMemory=1982
# PartitionName=debug Nodes=ALL Default=YES MaxTime=INFINITE State=UP
""")


def generate_slurm_conf(slurmctld_host, nodes, node_params):
    """Generate the slurm.conf based on the given nodes."""
    conf = [SLURM_CONF.substitute(slurmctld_host=slurmctld_host)]
    for node in nodes:
        conf.append('NodeName={} {}\n'.format(node, node_params[node]))
    # Just add a debug partition with everything for right now
    conf.append('PartitionName=debug Nodes=ALL Default=YES MaxTime=INFINITE State=UP')
    return ''.join(conf)


def setup_slurm(slurm_conf, munge_key, slurmctld_node=False):
    """Generate the slurm setup script."""
    setup = [
        # Install Slurm and deps
        'apt-get install -y slurmd slurmctld slurmrestd munge\n',
        'echo {} | base64 -d > /etc/munge/munge.key\n'.format(munge_key),
        # Create spool directories
        'mkdir /var/spool/slurmctld\n',
        'chown slurm:slurm /var/spool/slurmctld\n',
        # Start munge
        'systemctl start munge\n',
        'systemctl enable munge\n',
        # Configure slurm
        'cat >> /etc/slurm/slurm.conf <<EOF\n',
        slurm_conf,
        '\n',
        'EOF\n',
    ]
    if slurmctld_node:
        setup.append('systemctl start slurmctld\n')
        setup.append('systemctl enable slurmctld\n')
    else:
        setup.append('systemctl start slurmd\n')
        setup.append('systemctl enable slurmd\n')
    # TODO: Need to configure the /etc/hosts file
    # TODO: Proper NFS setup
    return ''.join(setup)


def generate_google_config(template_api, node_name, startup_script=None,
                           machine_type='n1-standard-1', src_image=SRC_IMAGE,
                           disk_size_gb=10):
    """Generate and return a based node config."""
    machine_str = 'zones/%s/machineTypes/%s' % (template_api.zone, machine_type)
    items = []
    if startup_script is not None:
        items.append({'key': 'startup-script', 'value': startup_script})
    return {
        'name': node_name,
        'machineType': machine_str,

        'disks': [
            {
                # Set the boot disk
                'boot': True,
                'autoDelete': True,
                'initializeParams': {
                    # Set the source image and disk size
                    'sourceImage': src_image,
                    'diskSizeGb': disk_size_gb,
                },
            }
        ],

        'networkInterfaces': [
            {
                # Public NAT IP
                'network': 'global/networks/default',
                'accessConfigs': [
                    {
                        'type': 'ONE_TO_ONE_NAT',
                        'name': 'External NAT',
                    },
                ]
            }
        ],

        'metadata': {
            'items': items,
        },
    }


def generate_base_script(user, password, pubkey, **kwargs):
    """Generate the base script."""
    ch_url = 'https://github.com/hpc/charliecloud/releases/download/v0.25/charliecloud-0.25.tar.gz'
    script = [
        # Install general deps
        'export DEBIAN_FRONTEND=noninteractive\n',
        'apt-get update\n',
        'apt-get install -y git curl vim tmux screen gcc make openmpi-bin libopenmpi-dev python3 python3-venv\n',
        # Install Charliecloud
        'cd /tmp\n',
        'curl -O -L {} || exit 1\n'.format(ch_url),
        'tar -xvf `basename {}`\n'.format(ch_url),
        'export DIR=`basename {} | rev | cut -d"." -f3- | rev`\n'.format(ch_url),
        'cd $DIR\n',
        './configure --prefix=/opt/$DIR || exit 1\n',
        'make && make install || exit 1\n',
        'cat > /etc/profile.d/charliecloud.sh <<EOF\n',
        'export PATH=/opt/$DIR/bin:\$PATH\n',
        'EOF\n',
        # Add the bee user
        'useradd -m -s /bin/bash {}\n'.format(user),
        'echo "{}:{}" | chpasswd\n'.format(user, password),
        'echo "%{} ALL=(ALL:ALL) NOPASSWD:ALL" > /etc/sudoers.d/bee\n'.format(user),
        'mkdir -p /home/{}/.ssh\n'.format(user),
        'echo {} | base64 -d > /home/{}/.ssh/authorized_keys\n'.format(pubkey, user),
        'chown {user}:{user} -R /home/bee\n'.format(user=user),
        # Enable user namespaces for debian
        '/sbin/sysctl kernel.unprivileged_userns_clone=1\n',
    ]
    return ''.join(script)


def generate_bee_setup(user, github_pat, git_branch, beeflow_wfm_listen_port,
                       beeflow_tm_listen_port, **kwargs):
    """Generate the BEE setup/installation script."""
    bee_dir = '/bee'
    bee_conf = '/bee/bee.conf'
    tm_script =  [
        '#!/bin/sh\n',
        '. /etc/profile\n',
        'cd /bee/BEE_Private\n',
        '. ./venv/bin/activate\n',
        'exec python -m beeflow.task_manager {}\n'.format(bee_conf),
    ]
    bee_conf = [
        '[DEFAULT]\n',
        'bee_workdir = /home/{}/.beeflow\n'.format(user),
        'workload_scheduler = Slurm\n',
        '[workflow_manager]\n',
        'listen_port = {}\n'.format(beeflow_wfm_listen_port),
        'log = /home/{}/.beeflow/logs/wfm.log\n'.format(user),
        '[task_manager]\n',
        'name = google-tm\n',
        'listen_port = {}\n'.format(beeflow_tm_listen_port),
        'container_runtime = Charliecloud\n',
        'log = /home/{}/.beeflow/logs/tm.log\n'.format(user),
        '[charliecloud]\n',
        'setup = module load charliecloud\n',
        'image_mntdir = /tmp\n',
        'chrun_opts = --cd /home/{}\n'.format(user),
        'container_dir = /home/{}\n'.format(user),
    ]
    setup = [
        'mkdir -p {}\n'.format(bee_dir),
        'cd {}\n'.format(bee_dir),
        # Clone the private repo
        'git clone https://{}:x-oauth-basic@github.com/lanl/BEE_Private.git || exit 1\n'.format(github_pat),
        'cd BEE_Private\n',
        'git checkout {}\n'.format(git_branch),
        # Install BEE in a venv with poetry
        'python3 -m venv venv\n',
        '. venv/bin/activate\n',
        'pip install --upgrade pip\n',
        'pip install poetry\n',
        'poetry update\n',
        'poetry install\n',
        # Output the bee.conf
        'cat >> /bee/bee.conf <<EOF\n',
        ''.join(bee_conf),
        'EOF\n',
        # Generate the startup script
        'cat >> /bee/tm <<EOF\n',
         ''.join(tm_script),
        'EOF\n',
        'chmod 755 /bee/tm\n',
        'chown -R {user}:{user} /bee\n'.format(user=user),
    ]

    return ''.join(setup)


def generate_wireguard_confs(ext_ips, wireguard, wireguard_port):
    """Generate the Wireguard configurations."""
    confs = {}
    for node in ext_ips:
        conf = [
            '[Interface]\n',
            'PrivateKey = {}\n'.format(key),
            'ListenPort = {}\n'.format(wireguard_port),
        ]
        for other_node in ext_ips:
            if node == other_node:
                continue
            pubkey = wireguard[other_node]['pubkey']
            psk = wireguard[other_node]['psk']
            ip = wireguard[other_node]['ip']
            ext_ip = ext_ips[other_node]
            conf.extend([
                '[Peer]\n',
                'PublicKey = {}\n'.format(pubkey),
                'PresharedKey = {}\n'.format(psk),
                'Endpoint = {}:9999\n'.format(ext_ip),
                'AllowedIPs = {}/32\n'.format(ip),
            ])
        confs[node] = ''.join(conf)
    return confs


def install_nfs(ext_ips, main_node, compute_nodes):
    """Install NFS on the cluster."""
    # Start the NFS server on the main node
    # TODO
    # Mount NFS on all the compute nodes
"""
if [ "$TYPE" = "main" ]; then
    echo "/home 10.93.78.0/24(rw,no_root_squash,subtree_check)" >> /etc/exports
    exportfs -a
    systemctl start nfs-server.service
else
    # Sleep some time on the nodes to allow for the main node setup
    sleep 120
    mount 10.93.78.4:/home /home
fi
"""


def generate_base_vpn_setup(wireguard, wg_conf, host):
    # Create a hosts file to append
    hosts = [
        '{} {}\n'.format(wireguard[node]['ip'], node)
        for node in wireguard
    ]
    hosts = ''.join(hosts)
    # Install and create the VPN
    setup = [
        '#!/bin/sh\n',
        'apt-get update\n',
        'apt-get install -y wireguard wireguard-tools\n',
        'cat > /etc/wireguard/wg0.conf <<EOF\n',
        wg_conf,
        'EOF\n',
        'chmod 600 /etc/wireguard/wg0.conf\n',
        'ip link add dev wg0 type wireguard\n',
        'ip addr add {}/24 dev wg0\n'.format(wireguard[host]['ip']),
        'sysctl net.ipv4.ip_forward=1\n',
        'wg setconf wg0 /etc/wireguard/wg0.conf\n',
        'ip link set up dev wg0\n',
         # Add to the hosts file
         'cat >> /etc/hosts <<EOF\n',
         hosts,
         'EOF\n',
    ]
    return ''.join(setup)


def generate_main_vpn_setup(wireguard, wireguard_port, main_node):
    """Generate the VPN setup script for the main node."""
    # Generate the wireguard config
    conf = [
        '[Interface]\n',
        'PrivateKey = {}\n'.format(wireguard[main_node]['key']),
        'ListenPort = {}\n'.format(wireguard_port),
    ]
    # Add each peer (the compute nodes)
    for comp in wireguard:
        if comp == main_node:
            continue
        conf.extend([
            '[Peer]\n',
            'PublicKey = {}\n'.format(wireguard[comp]['pubkey']),
            'PresharedKey = {}\n'.format(wireguard[comp]['psk']),
            'AllowedIPs = {}/32\n'.format(wireguard[comp]['ip']),
        ])
    wg_conf = ''.join(conf)
    return generate_base_vpn_setup(wireguard, wg_conf, main_node)


def generate_compute_vpn_setup(wireguard, wireguard_port, host, net_cidr, main,
                               main_ip):
    """Generate the VPN setup script for the compute nodes."""
    main_pubkey = wireguard[main]['pubkey']
    key = wireguard[host]['key']
    psk = wireguard[host]['psk']
    conf = [
        '[Interface]\n',
        'PrivateKey = {}\n'.format(key),
        '[Peer]\n',
        'PublicKey = {}\n'.format(main_pubkey),
        'PresharedKey = {}\n'.format(psk),
        'Endpoint = {}:{}\n'.format(main_ip, wireguard_port),
        'AllowedIPs = {}\n'.format(net_cidr),
        'PersistentKeepAlive = 50\n'
    ]
    wg_conf = ''.join(conf)
    return generate_base_vpn_setup(wireguard, wg_conf, host)


# TODO: Some of these arguments, like machine_type should be set by default in this template
def setup(template_api, node_name, startup_script=None,
          machine_type='n1-standard-1', src_image=SRC_IMAGE, disk_size_gb=10,
          wireguard=None, wireguard_port=None, net_cidr=None, munge_key=None,
          **kwargs):
    """Create a google node config and return it."""
    base_script = generate_base_script(**kwargs)

    # Generate the slurm config
    compute_nodes = ['bee-node0', 'bee-node1']
    node_params = {
        node: 'CPUs=1 Boards=1 SocketsPerBoard=1 CoresPerSocket=1 ThreadsPerCore=1 RealMemory=1982'
        for node in compute_nodes
    }
    slurm_conf = generate_slurm_conf('bee-main', compute_nodes, node_params)
    print('****')
    print('GENERATED SLURM CONF:')
    print(slurm_conf)
    print('****')

    # Create the slurmctld main node
    print('Creating bee-main node')
    main = 'bee-main'
    vpn_setup = generate_main_vpn_setup(wireguard, wireguard_port, main)
    bee_setup = generate_bee_setup(**kwargs)
    # Set up NFS on the server
    nfs_setup = ''.join([
        'apt-get update\n',
        'apt-get install -y nfs-kernel-server\n',
        'echo "/home {}(rw,no_root_squash,subtree_check)" >> /etc/exports\n'.format(net_cidr),
        'exportfs -a\n',
        'systemctl start nfs-server.service\n',
    ])
    setup = setup_slurm(slurm_conf, munge_key, slurmctld_node=True)
    startup_script = ''.join([vpn_setup, nfs_setup, base_script, setup, bee_setup])
    print(startup_script)

    cfg = generate_google_config(template_api, main,
                                 startup_script=startup_script)
    template_api.create_node(cfg)
    time.sleep(2)
    main_ip = template_api.get_ext_ip_addr(main)
    print('****')

    # Create each compute node
    for node in compute_nodes:
        print('Creating', node, 'node')
        vpn_setup = generate_compute_vpn_setup(wireguard, wireguard_port, node, net_cidr, main, main_ip)
        setup = setup_slurm(slurm_conf, munge_key)
        nfs_setup = ''.join([
             # Sleep some time on the nodes to allow for the main node setup
            'sleep 120',
            'mount {}:/home /home'.format(wireguard[main]['ip']),
        ])
        startup_script = ''.join(['#!/bin/sh\n', vpn_setup, base_script, setup, nfs_setup])
        cfg = generate_google_config(template_api, node,
                                     startup_script=startup_script)
        template_api.create_node(cfg)
        print('****')

    time.sleep(2)
    # TODO: This should be done with Google's VPC, but for now this will do.


def setup_cloud(provider):
    """Perform the general cloud setup."""
    setup(provider, **provider.params)
