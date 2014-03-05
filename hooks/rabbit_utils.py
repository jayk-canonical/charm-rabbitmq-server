import os
import pwd
import grp
import re
import sys
import subprocess
import glob
from lib.utils import render_template
import apt_pkg as apt

from charmhelpers.contrib.openstack.utils import (
    get_hostname,
)

from charmhelpers.core.hookenv import (
    config,
    relation_ids,
    relation_get,
    relation_set,
    related_units,
    local_unit,
    log, ERROR
)

PACKAGES = ['rabbitmq-server', 'python-amqplib']

RABBITMQ_CTL = '/usr/sbin/rabbitmqctl'
COOKIE_PATH = '/var/lib/rabbitmq/.erlang.cookie'
ENV_CONF = '/etc/rabbitmq/rabbitmq-env.conf'
RABBITMQ_CONF = '/etc/rabbitmq/rabbitmq.config'
RABBIT_USER = 'rabbitmq'
LIB_PATH = '/var/lib/rabbitmq/'


def vhost_exists(vhost):
    try:
        cmd = [RABBITMQ_CTL, 'list_vhosts']
        out = subprocess.check_output(cmd)
        for line in out.split('\n')[1:]:
            if line == vhost:
                log('vhost (%s) already exists.' % vhost)
                return True
        return False
    except:
        # if no vhosts, just raises an exception
        return False


def create_vhost(vhost):
    if vhost_exists(vhost):
        return
    cmd = [RABBITMQ_CTL, 'add_vhost', vhost]
    subprocess.check_call(cmd)
    log('Created new vhost (%s).' % vhost)


def user_exists(user):
    cmd = [RABBITMQ_CTL, 'list_users']
    out = subprocess.check_output(cmd)
    for line in out.split('\n')[1:]:
        _user = line.split('\t')[0]
        if _user == user:
            admin = line.split('\t')[1]
            return True, (admin == '[administrator]')
    return False, False


def create_user(user, password, admin=False):
    exists, is_admin = user_exists(user)

    if not exists:
        cmd = [RABBITMQ_CTL, 'add_user', user, password]
        subprocess.check_call(cmd)
        log('Created new user (%s).' % user)

    if admin == is_admin:
        return

    if admin:
        cmd = [RABBITMQ_CTL, 'set_user_tags', user, 'administrator']
        log('Granting user (%s) admin access.')
    else:
        cmd = [RABBITMQ_CTL, 'set_user_tags', user]
        log('Revoking user (%s) admin access.')


def grant_permissions(user, vhost):
    cmd = [RABBITMQ_CTL, 'set_permissions', '-p',
           vhost, user, '.*', '.*', '.*']
    subprocess.check_call(cmd)


def service(action):
    cmd = ['service', 'rabbitmq-server', action]
    subprocess.check_call(cmd)


def compare_version(base_version):
    apt.init()
    cache = apt.Cache()
    pkg = cache['rabbitmq-server']
    if pkg.current_ver:
        return apt.version_compare(
            apt.upstream_version(pkg.current_ver.ver_str),
            base_version)
    else:
        return False


def cluster_with():
    log('Clustering with new node')
    if compare_version('3.0.1') >= 0:
        cluster_cmd = 'join_cluster'
    else:
        cluster_cmd = 'cluster'
    out = subprocess.check_output([RABBITMQ_CTL, 'cluster_status'])
    log('cluster status is %s' % str(out))

    # check if node is already clustered
    total_nodes = 1
    running_nodes = []
    m = re.search("\{running_nodes,\[(.*)\]\}", out.strip())
    if m is not None:
        running_nodes = m.group(1).split(',')
        running_nodes = [x.replace("'", '') for x in running_nodes]
        total_nodes = len(running_nodes)

    if total_nodes > 1:
        log('Node is already clustered, skipping')
        return False

    # check all peers and try to cluster with them
    available_nodes = []
    for r_id in relation_ids('cluster'):
        for unit in related_units(r_id):
            address = relation_get('private-address',
                                   rid=r_id, unit=unit)
            if address is not None:
                node = get_hostname(address, fqdn=False)
                available_nodes.append(node)

    if len(available_nodes) == 0:
        log('No nodes available to cluster with')
        return False

    # iterate over all the nodes, join to the first available
    num_tries = 0
    for node in available_nodes:
        log('Clustering with remote rabbit host (%s).' % node)
        if node in running_nodes:
            log('Host already clustered with %s.' % node)
            return False

        try:
            cmd = [RABBITMQ_CTL, 'stop_app']
            subprocess.check_call(cmd)
            cmd = [RABBITMQ_CTL, cluster_cmd, 'rabbit@%s' % node]
            subprocess.check_call(cmd)
            cmd = [RABBITMQ_CTL, 'start_app']
            subprocess.check_call(cmd)
            log('Host clustered with %s.' % node)
            if compare_version('3.0.1') >= 0:
                cmd = [RABBITMQ_CTL, 'set_policy', 'HA',
                       '^(?!amq\.).*', '{"ha-mode": "all"}']
                subprocess.check_call(cmd)
            return True
        except:
            log('Failed to cluster with %s.' % node)
        # continue to the next node
        num_tries += 1
        if num_tries > config('max-cluster-tries'):
            log('Max tries number exhausted, exiting', level=ERROR)
            raise

    return False


def break_cluster():
    try:
        cmd = [RABBITMQ_CTL, 'stop_app']
        subprocess.check_call(cmd)
        cmd = [RABBITMQ_CTL, 'reset']
        subprocess.check_call(cmd)
        cmd = [RABBITMQ_CTL, 'start_app']
        subprocess.check_call(cmd)
        log('Cluster successfully broken.')
    except:
        # error, no nodes available for clustering
        log('Error breaking rabbit cluster', level=ERROR)
        raise


def set_node_name(name):
    # update or append RABBITMQ_NODENAME to environment config.
    # rabbitmq.conf.d is not present on all releases, so use or create
    # rabbitmq-env.conf instead.
    if not os.path.isfile(ENV_CONF):
        log('%s does not exist, creating.' % ENV_CONF)
        with open(ENV_CONF, 'wb') as out:
            out.write('RABBITMQ_NODENAME=%s\n' % name)
        return

    out = []
    f = False
    for line in open(ENV_CONF).readlines():
        if line.strip().startswith('RABBITMQ_NODENAME'):
            f = True
            line = 'RABBITMQ_NODENAME=%s\n' % name
        out.append(line)
    if not f:
        out.append('RABBITMQ_NODENAME=%s\n' % name)
    log('Updating %s, RABBITMQ_NODENAME=%s' %
        (ENV_CONF, name))
    with open(ENV_CONF, 'wb') as conf:
        conf.write(''.join(out))


def get_node_name():
    if not os.path.exists(ENV_CONF):
        return None
    env_conf = open(ENV_CONF, 'r').readlines()
    node_name = None
    for l in env_conf:
        if l.startswith('RABBITMQ_NODENAME'):
            node_name = l.split('=')[1].strip()
    return node_name


def _manage_plugin(plugin, action):
    os.environ['HOME'] = '/root'
    _rabbitmq_plugins = \
        glob.glob('/usr/lib/rabbitmq/lib/rabbitmq_server-*'
                  '/sbin/rabbitmq-plugins')[0]
    subprocess.check_call([_rabbitmq_plugins, action, plugin])


def enable_plugin(plugin):
    _manage_plugin(plugin, 'enable')


def disable_plugin(plugin):
    _manage_plugin(plugin, 'disable')

ssl_key_file = "/etc/rabbitmq/rabbit-server-privkey.pem"
ssl_cert_file = "/etc/rabbitmq/rabbit-server-cert.pem"
ssl_ca_file = "/etc/rabbitmq/rabbit-server-ca.pem"


def enable_ssl(ssl_key, ssl_cert, ssl_port,
               ssl_ca=None, ssl_only=False, ssl_client=None):
    uid = pwd.getpwnam("root").pw_uid
    gid = grp.getgrnam("rabbitmq").gr_gid

    for contents, path in (
            (ssl_key, ssl_key_file),
            (ssl_cert, ssl_cert_file),
            (ssl_ca, ssl_ca_file)):
        if not contents:
            continue
        with open(path, 'w') as fh:
            fh.write(contents)
        os.chmod(path, 0o640)
        os.chown(path, uid, gid)

    data = {
        "ssl_port": ssl_port,
        "ssl_cert_file": ssl_cert_file,
        "ssl_key_file": ssl_key_file,
        "ssl_client": ssl_client,
        "ssl_ca_file": "",
        "ssl_only": ssl_only}

    if ssl_ca:
        data["ssl_ca_file"] = ssl_ca_file

    with open(RABBITMQ_CONF, 'w') as rmq_conf:
        rmq_conf.write(render_template(
            os.path.basename(RABBITMQ_CONF), data))


def execute(cmd, die=False, echo=False):
    """ Executes a command

    if die=True, script will exit(1) if command does not return 0
    if echo=True, output of command will be printed to stdout

    returns a tuple: (stdout, stderr, return code)
    """
    p = subprocess.Popen(cmd.split(" "),
                         stdout=subprocess.PIPE,
                         stdin=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    stdout = ""
    stderr = ""

    def print_line(l):
        if echo:
            print l.strip('\n')
            sys.stdout.flush()

    for l in iter(p.stdout.readline, ''):
        print_line(l)
        stdout += l
    for l in iter(p.stderr.readline, ''):
        print_line(l)
        stderr += l

    p.communicate()
    rc = p.returncode

    if die and rc != 0:
        log("command %s return non-zero." % cmd, level=ERROR)
    return (stdout, stderr, rc)


def get_clustered_attribute(attribute_name):
    cluster_rels = relation_ids('cluster')
    if len(cluster_rels) > 0:
        cluster_rid = cluster_rels[0]
        password = relation_get(
            attribute=attribute_name,
            rid=cluster_rid,
            unit=local_unit())
        return password
    else:
        return None


def set_clustered_attribute(attribute_name, value):
    cluster_rels = relation_ids('cluster')
    if len(cluster_rels) > 0:
        cluster_rid = cluster_rels[0]
        relation_set(
            relation_id=cluster_rid,
            relation_settings={attribute_name: value})
