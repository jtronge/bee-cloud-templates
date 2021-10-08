#!/bin/sh
# OpenStack BEE install script

# Install Charliecloud
install_charliecloud()
{
    local url=$1
    cd /tmp
    curl -O -L $url || exit 1
    tar -xvf `basename $url`
    local dir=`basename $url | rev | cut -d'.' -f3- | rev`
    cd $dir
    ./configure --prefix=/opt/$dir || exit 1
    make && make install || exit 1
    cat > /etc/profile.d/charliecloud.sh <<EOF
export PATH=/opt/$dir/bin:\$PATH
EOF
}

# Install BEE + dependencies
install_bee()
{
    local auth_token=$1
    local bee_dir=$2
    local conf=$3
    local git_branch=$4
    mkdir -p $bee_dir
    cd $bee_dir
    git clone https://$auth_token:x-oauth-basic@github.com/lanl/BEE_Private.git || exit 1
    cd BEE_Private
    git checkout $git_branch
    python3 -m venv venv
    . venv/bin/activate
    pip install --upgrade pip
    pip install poetry
    poetry update
    poetry install

    # Generate TM init script
    # TODO: May need to start Redis here eventually
    cat >> /bee/tm <<EOF
#!/bin/sh
. /etc/profile
cd /bee/BEE_Private
. ./venv/bin/activate
exec python -m beeflow.task_manager $conf
EOF
    chmod 755 /bee/tm
}

gen_conf()
{
    # local conf=/home/bee/.config/beeflow/bee.conf
    local conf=$1
    local wfm_listen_port=$2
    local tm_listen_port=$3
    cat >> $conf <<EOF
[DEFAULT]
bee_workdir = /home/$USER/.beeflow
workload_scheduler = Simple
[workflow_manager]
listen_port = $wfm_listen_port
log = /home/$USER/.beeflow/logs/wfm.log
[task_manager]
name = dora-tm
listen_port = $tm_listen_port
container_runtime = Charliecloud
log = /home/$USER/.beeflow/logs/tm.log
[charliecloud]
setup = module load charliecloud
image_mntdir = /tmp
chrun_opts = --cd /home/$USER
container_dir = /home/$USER
EOF
}

# Setup Dora specific proxy and nameservers
cat > /etc/profile.d/proxy.sh <<EOF
export https_proxy=$HTTPS_PROXY
export http_proxy=$HTTP_PROXY
export no_proxy=$NO_PROXY
EOF
echo "" > /etc/resolv.conf
for ns in `echo $NAMESERVERS | tr ',' ' '`; do
    printf "nameserver $ns\n" >> /etc/resolv.conf
done
. /etc/profile

# Install general deps
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git curl vim tmux screen gcc make openmpi-bin libopenmpi-dev python3 python3-venv

# Enable user namespaces
/sbin/sysctl kernel.unprivileged_userns_clone=1

install_charliecloud https://github.com/hpc/charliecloud/releases/download/v0.24/charliecloud-0.24.tar.gz
install_bee $GITHUB_PAT /bee /bee/bee.conf $GIT_BRANCH
gen_conf /bee/bee.conf $WFM_LISTEN_PORT $TM_LISTEN_PORT

chown -R $USER:$USER /bee

# Install Slurm and deps
apt-get install -y slurmd slurmctld slurmrestd munge
# Install the munge key
echo $MUNGE_KEY | base64 -d > /etc/munge/munge.key
# Make spool directories
mkdir /var/spool/slurmctld
chown slurm:slurm /var/spool/slurmctld
# Start and enable munge
systemctl start munge
systemctl enable munge
# Configure Slurm
cat >> /etc/slurm/slurm.conf <<EOF
ClusterName=dora-bee
SlurmctldHost=bee-main

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

NodeName=bee-main CPUs=1 Boards=1 SocketsPerBoard=1 CoresPerSocket=1 ThreadsPerCore=1 RealMemory=1982
NodeName=bee-node0 CPUs=1 Boards=1 SocketsPerBoard=1 CoresPerSocket=1 ThreadsPerCore=1 RealMemory=1982
PartitionName=debug Nodes=ALL Default=YES MaxTime=INFINITE State=UP
EOF
# Slurmctld only should be started on the main node
if [ "$TYPE" = "main" ]; then
    systemctl start slurmctld
    systemctl enable slurmctld
fi
systemctl start slurmd
systemctl enable slurmd

cat >> /etc/hosts << EOF
127.0.0.1		localhost localhost.localdomain
::1			localhost localhost.localdomain

10.93.78.4		bee-main
10.93.78.5		bee-node0
EOF

# Set up NFS
apt-get install -y nfs-kernel-server
if [ "$TYPE" = "main" ]; then
    echo "/home 10.93.78.0/24(rw,no_root_squash,subtree_check)" >> /etc/exports
    exportfs -a
    systemctl start nfs-server.service
else
    # Sleep some time on the nodes to allow for the main node setup
    sleep 120
    mount 10.93.78.4:/home /home
fi
