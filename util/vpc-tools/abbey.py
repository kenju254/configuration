#!/usr/bin/env python -u
import sys
from argparse import ArgumentParser
import time
import json
import yaml
import os
try:
    import boto.ec2
    import boto.sqs
    from boto.vpc import VPCConnection
    from boto.exception import NoAuthHandlerFound, EC2ResponseError
    from boto.sqs.message import RawMessage
except ImportError:
    print "boto required for script"
    sys.exit(1)

from pprint import pprint

AMI_TIMEOUT = 600  # time to wait for AMIs to complete
EC2_RUN_TIMEOUT = 180  # time to wait for ec2 state transition
EC2_STATUS_TIMEOUT = 300  # time to wait for ec2 system status checks
NUM_TASKS = 5  # number of tasks for time summary report
NUM_PLAYBOOKS = 2


class Unbuffered:
    """
    For unbuffered output, not
    needed if PYTHONUNBUFFERED is set
    """
    def __init__(self, stream):
        self.stream = stream

    def write(self, data):
        self.stream.write(data)
        self.stream.flush()

    def __getattr__(self, attr):
        return getattr(self.stream, attr)

sys.stdout = Unbuffered(sys.stdout)


def parse_args():
    parser = ArgumentParser()
    parser.add_argument('--noop', action='store_true',
                        help="don't actually run the cmds",
                        default=False)
    parser.add_argument('--secure-vars', required=False,
                        metavar="SECURE_VAR_FILE",
                        help="path to secure-vars from the root of "
                        "the secure repo (defaults to ansible/"
                        "vars/ENVIRONMENT-DEPLOYMENT.yml)")
    parser.add_argument('--stack-name',
                        help="defaults to ENVIRONMENT-DEPLOYMENT",
                        metavar="STACK_NAME",
                        required=False)
    parser.add_argument('-p', '--play',
                        help='play name without the yml extension',
                        metavar="PLAY", required=True)
    parser.add_argument('--playbook-dir',
                        help='directory to find playbooks in',
                        default='configuration/playbooks/edx-east',
                        metavar="PLAYBOOKDIR", required=False)
    parser.add_argument('-d', '--deployment', metavar="DEPLOYMENT",
                        required=True)
    parser.add_argument('-e', '--environment', metavar="ENVIRONMENT",
                        required=True)
    parser.add_argument('-v', '--verbose', action='store_true',
                        help="turn on verbosity")
    parser.add_argument('--no-cleanup', action='store_true',
                        help="don't cleanup on failures")
    parser.add_argument('--vars', metavar="EXTRA_VAR_FILE",
                        help="path to extra var file", required=False)
    parser.add_argument('--refs', metavar="GIT_REFS_FILE",
                        help="path to a var file with app git refs", required=False)
    parser.add_argument('--configuration-version', required=False,
                        help="configuration repo branch(no hashes)",
                        default="master")
    parser.add_argument('--configuration-secure-version', required=False,
                        help="configuration-secure repo branch(no hashes)",
                        default="master")
    parser.add_argument('--configuration-secure-repo', required=False,
                        default="git@github.com:edx-ops/prod-secure",
                        help="repo to use for the secure files")
    parser.add_argument('--configuration-private-version', required=False,
                        help="configuration-private repo branch(no hashes)",
                        default="master")
    parser.add_argument('--configuration-private-repo', required=False,
                        default="git@github.com:edx-ops/ansible-private",
                        help="repo to use for private playbooks")
    parser.add_argument('-c', '--cache-id', required=True,
                        help="unique id to use as part of cache prefix")
    parser.add_argument('-i', '--identity', required=False,
                        help="path to identity file for pulling "
                             "down configuration-secure",
                        default=None)
    parser.add_argument('-r', '--region', required=False,
                        default="us-east-1",
                        help="aws region")
    parser.add_argument('-k', '--keypair', required=False,
                        default="deployment",
                        help="AWS keypair to use for instance")
    parser.add_argument('-t', '--instance-type', required=False,
                        default="m1.large",
                        help="instance type to launch")
    parser.add_argument("--role-name", required=False,
                        default="abbey",
                        help="IAM role name to use (must exist)")
    parser.add_argument("--msg-delay", required=False,
                        default=5,
                        help="How long to delay message display from sqs "
                             "to ensure ordering")
    parser.add_argument("--hipchat-room-id", required=False,
                        default=None,
                        help="The API ID of the Hipchat room to post"
                             "status messages to")
    parser.add_argument("--hipchat-api-token", required=False,
                        default=None,
                        help="The API token for Hipchat integration")

    group = parser.add_mutually_exclusive_group()
    group.add_argument('-b', '--base-ami', required=False,
                        help="ami to use as a base ami",
                        default="ami-0568456c")
    group.add_argument('--blessed', action='store_true',
                        help="Look up blessed ami for env-dep-play.",
                        default=False)

    return parser.parse_args()


def get_instance_sec_group(vpc_id):

    grp_details = ec2.get_all_security_groups(
        filters={
            'vpc_id': vpc_id,
            'tag:play': args.play
        }
    )

    if len(grp_details) < 1:
        sys.stderr.write("ERROR: Expected atleast one security group, got {}\n".format(
            len(grp_details)))

    return grp_details[0].id

def get_blessed_ami():
    images = ec2.get_all_images(
        filters={
            'tag:environment': args.environment,
            'tag:deployment': args.deployment,
            'tag:play': args.play,
            'tag:blessed': True
        }
    )

    if len(images) != 1:
        raise Exception("ERROR: Expected only one blessed ami, got {}\n".format(
            len(images)))

    return images[0].id

def create_instance_args():
    """
    Looks up security group, subnet
    and returns arguments to pass into
    ec2.run_instances() including
    user data
    """

    vpc = VPCConnection()
    subnet = vpc.get_all_subnets(
        filters={
            'tag:aws:cloudformation:stack-name': stack_name,
            'tag:play': args.play}
    )
    if len(subnet) < 1:
        sys.stderr.write("ERROR: Expected at least one subnet, got {}\n".format(
            len(subnet)))
        sys.exit(1)
    subnet_id = subnet[0].id
    vpc_id = subnet[0].vpc_id

    security_group_id = get_instance_sec_group(vpc_id)

    if args.identity:
        config_secure = 'true'
        with open(args.identity) as f:
            identity_contents = f.read()
    else:
        config_secure = 'false'
        identity_contents = "dummy"

    user_data = """#!/bin/bash
set -x
set -e
exec > >(tee /var/log/user-data.log|logger -t user-data -s 2>/dev/console) 2>&1
base_dir="/var/tmp/edx-cfg"
extra_vars="$base_dir/extra-vars-$$.yml"
secure_identity="$base_dir/secure-identity"
git_ssh="$base_dir/git_ssh.sh"
configuration_version="{configuration_version}"
configuration_secure_version="{configuration_secure_version}"
configuration_private_version="{configuration_private_version}"
environment="{environment}"
deployment="{deployment}"
play="{play}"
config_secure={config_secure}
git_repo_name="configuration"
git_repo="https://github.com/edx/$git_repo_name"
git_repo_secure="{configuration_secure_repo}"
git_repo_secure_name="{configuration_secure_repo_basename}"
git_repo_private="{configuration_private_repo}"
git_repo_private_name=$(basename $git_repo_private .git)
secure_vars_file="$base_dir/$git_repo_secure_name/{secure_vars}"
instance_id=\\
$(curl http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null)
instance_ip=\\
$(curl http://169.254.169.254/latest/meta-data/local-ipv4 2>/dev/null)
instance_type=\\
$(curl http://169.254.169.254/latest/meta-data/instance-type 2>/dev/null)
playbook_dir="$base_dir/{playbook_dir}"

if $config_secure; then
    git_cmd="env GIT_SSH=$git_ssh git"
else
    git_cmd="git"
fi

ANSIBLE_ENABLE_SQS=true
SQS_NAME={queue_name}
SQS_REGION=us-east-1
SQS_MSG_PREFIX="[ $instance_id $instance_ip $environment-$deployment $play ]"
PYTHONUNBUFFERED=1

# environment for ansible
export ANSIBLE_ENABLE_SQS SQS_NAME SQS_REGION SQS_MSG_PREFIX PYTHONUNBUFFERED

if [[ ! -x /usr/bin/git || ! -x /usr/bin/pip ]]; then
    echo "Installing pkg dependencies"
    /usr/bin/apt-get update
    /usr/bin/apt-get install -y git python-pip python-apt \\
        git-core build-essential python-dev libxml2-dev \\
        libxslt-dev curl --force-yes
fi


rm -rf $base_dir
mkdir -p $base_dir
cd $base_dir

cat << EOF > $git_ssh
#!/bin/sh
exec /usr/bin/ssh -o StrictHostKeyChecking=no -i "$secure_identity" "\$@"
EOF

chmod 755 $git_ssh

if $config_secure; then
    cat << EOF > $secure_identity
{identity_contents}
EOF
fi

cat << EOF >> $extra_vars
---
# extra vars passed into
# abbey.py including versions
# of all the repositories
{extra_vars_yml}

{git_refs_yml}

# abbey will always run fake migrations
# this is so that the application can come
# up healthy
fake_migrations: true

# Use the build number an the dynamic cache key.
EDXAPP_UPDATE_STATIC_FILES_KEY: true
edxapp_dynamic_cache_key: {deployment}-{environment}-{play}-{cache_id}

disable_edx_services: true
EOF

chmod 400 $secure_identity

$git_cmd clone $git_repo $git_repo_name
cd $git_repo_name
$git_cmd checkout $configuration_version
cd $base_dir

if $config_secure; then
    $git_cmd clone $git_repo_secure $git_repo_secure_name
    cd $git_repo_secure_name
    $git_cmd checkout $configuration_secure_version
    cd $base_dir
fi

if [[ ! -z $git_repo_private ]]; then
    $git_cmd clone $git_repo_private $git_repo_private_name
    cd $git_repo_private_name
    $git_cmd checkout $configuration_private_version
    cd $base_dir
fi


cd $base_dir/$git_repo_name
sudo pip install -r requirements.txt

cd $playbook_dir

ansible-playbook -vvvv -c local -i "localhost," $play.yml -e@$secure_vars_file -e@$extra_vars
ansible-playbook -vvvv -c local -i "localhost," stop_all_edx_services.yml -e@$secure_vars_file -e@$extra_vars

rm -rf $base_dir

    """.format(
                configuration_version=args.configuration_version,
                configuration_secure_version=args.configuration_secure_version,
                configuration_secure_repo=args.configuration_secure_repo,
                configuration_secure_repo_basename=os.path.basename(
                    args.configuration_secure_repo),
                configuration_private_version=args.configuration_private_version,
                configuration_private_repo=args.configuration_private_repo,
                environment=args.environment,
                deployment=args.deployment,
                play=args.play,
                playbook_dir=args.playbook_dir,
                config_secure=config_secure,
                identity_contents=identity_contents,
                queue_name=run_id,
                extra_vars_yml=extra_vars_yml,
                git_refs_yml=git_refs_yml,
                secure_vars=secure_vars,
                cache_id=args.cache_id)

    ec2_args = {
        'security_group_ids': [security_group_id],
        'subnet_id': subnet_id,
        'key_name': args.keypair,
        'image_id': base_ami,
        'instance_type': args.instance_type,
        'instance_profile_name': args.role_name,
        'user_data': user_data,

    }

    return ec2_args


def poll_sqs_ansible():
    """
    Prints events to the console and
    blocks until a final STATS ansible
    event is read off of SQS.

    SQS does not guarantee FIFO, for that
    reason there is a buffer that will delay
    messages before they are printed to the
    console.

    Returns length of the ansible run.
    """
    oldest_msg_ts = 0
    buf = []
    task_report = []  # list of tasks for reporting
    last_task = None
    completed = 0
    while True:
        messages = []
        while True:
            # get all available messages on the queue
            msgs = sqs_queue.get_messages(attributes='All')
            if not msgs:
                break
            messages.extend(msgs)

        for message in messages:
            recv_ts = float(
                message.attributes['ApproximateFirstReceiveTimestamp']) * .001
            sent_ts = float(message.attributes['SentTimestamp']) * .001
            try:
                msg_info = {
                    'msg': json.loads(message.get_body()),
                    'sent_ts': sent_ts,
                    'recv_ts': recv_ts,
                }
                buf.append(msg_info)
            except ValueError as e:
                print "!!! ERROR !!! unable to parse queue message, " \
                      "expecting valid json: {} : {}".format(
                          message.get_body(), e)
            if not oldest_msg_ts or recv_ts < oldest_msg_ts:
                oldest_msg_ts = recv_ts
            sqs_queue.delete_message(message)

        now = int(time.time())
        if buf:
            try:
                if (now - max([msg['recv_ts'] for msg in buf])) > args.msg_delay:
                    # sort by TS instead of recv_ts
                    # because the sqs timestamp is not as
                    # accurate
                    buf.sort(key=lambda k: k['msg']['TS'])
                    to_disp = buf.pop(0)
                    if 'START' in to_disp['msg']:
                        print '\n{:0>2.0f}:{:0>5.2f} {} : Starting "{}"'.format(
                            to_disp['msg']['TS'] / 60,
                            to_disp['msg']['TS'] % 60,
                            to_disp['msg']['PREFIX'],
                            to_disp['msg']['START']),

                    elif 'TASK' in to_disp['msg']:
                        print "\n{:0>2.0f}:{:0>5.2f} {} : {}".format(
                            to_disp['msg']['TS'] / 60,
                            to_disp['msg']['TS'] % 60,
                            to_disp['msg']['PREFIX'],
                            to_disp['msg']['TASK']),
                        last_task = to_disp['msg']['TASK']
                    elif 'OK' in to_disp['msg']:
                        if args.verbose:
                            print "\n"
                            for key, value in to_disp['msg']['OK'].iteritems():
                                print "    {:<15}{}".format(key, value)
                        else:
                            invocation = to_disp['msg']['OK']['invocation']
                            module = invocation['module_name']
                            # 'set_fact' does not provide a changed value.
                            if module == 'set_fact':
                                changed = "OK"
                            elif to_disp['msg']['OK']['changed']:
                                changed = "*OK*"
                            else:
                                changed = "OK"
                            print " {}".format(changed),
                        task_report.append({
                            'TASK': last_task,
                            'INVOCATION': to_disp['msg']['OK']['invocation'],
                            'DELTA': to_disp['msg']['delta'],
                        })
                    elif 'FAILURE' in to_disp['msg']:
                        print " !!!! FAILURE !!!!",
                        for key, value in to_disp['msg']['FAILURE'].iteritems():
                            print "    {:<15}{}".format(key, value)
                        raise Exception("Failed Ansible run")
                    elif 'STATS' in to_disp['msg']:
                        print "\n{:0>2.0f}:{:0>5.2f} {} : COMPLETE".format(
                            to_disp['msg']['TS'] / 60,
                            to_disp['msg']['TS'] % 60,
                            to_disp['msg']['PREFIX'])

                        # Since 3 ansible plays get run.
                        # We see the COMPLETE message 3 times
                        # wait till the last one to end listening
                        # for new messages.
                        completed += 1
                        if completed >= NUM_PLAYBOOKS:
                            return (to_disp['msg']['TS'], task_report)
            except KeyError:
                print "Failed to print status from message: {}".format(to_disp)

        if not messages:
            # wait 1 second between sqs polls
            time.sleep(1)


def create_ami(instance_id, name, description):

    params = {'instance_id': instance_id,
              'name': name,
              'description': description,
              'no_reboot': True}

    AWS_API_WAIT_TIME = 1
    image_id = ec2.create_image(**params)
    print("Checking if image is ready.")
    for _ in xrange(AMI_TIMEOUT):
        try:
            img = ec2.get_image(image_id)
            if img.state == 'available':
                print("Tagging image.")
                img.add_tag("environment", args.environment)
                time.sleep(AWS_API_WAIT_TIME)
                img.add_tag("deployment", args.deployment)
                time.sleep(AWS_API_WAIT_TIME)
                img.add_tag("play", args.play)
                time.sleep(AWS_API_WAIT_TIME)
                img.add_tag("configuration_ref", args.configuration_version)
                time.sleep(AWS_API_WAIT_TIME)
                img.add_tag("configuration_secure_ref", args.configuration_secure_version)
                time.sleep(AWS_API_WAIT_TIME)
                img.add_tag("configuration_secure_repo", args.configuration_secure_repo)
                time.sleep(AWS_API_WAIT_TIME)
                img.add_tag("cache_id", args.cache_id)
                time.sleep(AWS_API_WAIT_TIME)
                for repo, ref in git_refs.items():
                    key = "refs:{}".format(repo)
                    img.add_tag(key, ref)
                    time.sleep(AWS_API_WAIT_TIME)
                break
            else:
                time.sleep(1)
        except EC2ResponseError as e:
            if e.error_code == 'InvalidAMIID.NotFound':
                time.sleep(1)
            else:
                raise Exception("Unexpected error code: {}".format(
                    e.error_code))
            time.sleep(1)
    else:
        raise Exception("Timeout waiting for AMI to finish")

    return image_id

def launch_and_configure(ec2_args):
    """
    Creates an sqs queue, launches an ec2 instance,
    configures it and creates an AMI. Polls
    SQS for updates
    """

    print "{:<40}".format(
        "Creating SQS queue and launching instance for {}:".format(run_id))
    print
    for k, v in ec2_args.iteritems():
        if k != 'user_data':
            print "    {:<25}{}".format(k, v)
    print

    global sqs_queue
    global instance_id
    sqs_queue = sqs.create_queue(run_id)
    sqs_queue.set_message_class(RawMessage)
    res = ec2.run_instances(**ec2_args)
    inst = res.instances[0]
    instance_id = inst.id

    print "{:<40}".format(
        "Waiting for instance {} to reach running status:".format(instance_id)),
    status_start = time.time()
    for _ in xrange(EC2_RUN_TIMEOUT):
        res = ec2.get_all_instances(instance_ids=[instance_id])
        if res[0].instances[0].state == 'running':
            status_delta = time.time() - status_start
            run_summary.append(('EC2 Launch', status_delta))
            print "[ OK ] {:0>2.0f}:{:0>2.0f}".format(
                status_delta / 60,
                status_delta % 60)
            break
        else:
            time.sleep(1)
    else:
        raise Exception("Timeout waiting for running status: {} ".format(
            instance_id))

    print "{:<40}".format("Waiting for system status:"),
    system_start = time.time()
    for _ in xrange(EC2_STATUS_TIMEOUT):
        status = ec2.get_all_instance_status(inst.id)
        if status[0].system_status.status == u'ok':
            system_delta = time.time() - system_start
            run_summary.append(('EC2 Status Checks', system_delta))
            print "[ OK ] {:0>2.0f}:{:0>2.0f}".format(
                system_delta / 60,
                system_delta % 60)
            break
        else:
            time.sleep(1)
    else:
        raise Exception("Timeout waiting for status checks: {} ".format(
            instance_id))

    print
    print "{:<40}".format(
        "Waiting for user-data, polling sqs for Ansible events:")

    (ansible_delta, task_report) = poll_sqs_ansible()
    run_summary.append(('Ansible run', ansible_delta))
    print
    print "{} longest Ansible tasks (seconds):".format(NUM_TASKS)
    for task in sorted(
            task_report, reverse=True,
            key=lambda k: k['DELTA'])[:NUM_TASKS]:
        print "{:0>3.0f} {}".format(task['DELTA'], task['TASK'])
        print "  - {}".format(task['INVOCATION'])
    print

    print "{:<40}".format("Creating AMI:"),
    ami_start = time.time()
    ami = create_ami(instance_id, run_id, run_id)
    ami_delta = time.time() - ami_start
    print "[ OK ] {:0>2.0f}:{:0>2.0f}".format(
        ami_delta / 60,
        ami_delta % 60)
    run_summary.append(('AMI Build', ami_delta))
    total_time = time.time() - start_time
    all_stages = sum(run[1] for run in run_summary)
    if total_time - all_stages > 0:
        run_summary.append(('Other', total_time - all_stages))
    run_summary.append(('Total', total_time))

    return run_summary, ami

def send_hipchat_message(message):
    #If hipchat is configured send the details to the specified room
    if args.hipchat_api_token and args.hipchat_room_id:
        import hipchat
        try:
            hipchat = hipchat.HipChat(token=args.hipchat_api_token)
            hipchat.message_room(args.hipchat_room_id,'AbbeyNormal',
               message)
        except Exception as e:
            print("Hipchat messaging resulted in an error: %s." % e)

if __name__ == '__main__':

    args = parse_args()

    run_summary = []

    start_time = time.time()

    if args.vars:
        with open(args.vars) as f:
            extra_vars_yml = f.read()
            extra_vars = yaml.load(extra_vars_yml)
    else:
        extra_vars_yml = ""
        extra_vars = {}

    if args.refs:
        with open(args.refs) as f:
            git_refs_yml = f.read()
            git_refs = yaml.load(git_refs_yml)
    else:
        git_refs_yml = ""
        git_refs = {}

    if args.secure_vars:
        secure_vars = args.secure_vars
    else:
        secure_vars = "ansible/vars/{}-{}.yml".format(
                      args.environment, args.deployment)
    if args.stack_name:
        stack_name = args.stack_name
    else:
        stack_name = "{}-{}".format(args.environment, args.deployment)

    try:
        sqs = boto.sqs.connect_to_region(args.region)
        ec2 = boto.ec2.connect_to_region(args.region)
    except NoAuthHandlerFound:
        print 'You must be able to connect to sqs and ec2 to use this script'
        sys.exit(1)

    if args.blessed:
        base_ami = get_blessed_ami()
    else:
        base_ami = args.base_ami

    try:
        sqs_queue = None
        instance_id = None

        run_id = "{}-abbey-{}-{}-{}".format(
            int(time.time() * 100), args.environment, args.deployment, args.play)

        ec2_args = create_instance_args()

        if args.noop:
            print "Would have created sqs_queue with id: {}\nec2_args:".format(
                run_id)
            pprint(ec2_args)
            ami = "ami-00000"
        else:
            run_summary, ami = launch_and_configure(ec2_args)
            print
            print "Summary:\n"

            for run in run_summary:
                print "{:<30} {:0>2.0f}:{:0>5.2f}".format(
                    run[0], run[1] / 60, run[1] % 60)
            print "AMI: {}".format(ami)

            message = 'Finished baking AMI {image_id} for {environment} ' \
              '{deployment} {play}.'.format(
                    image_id=ami,
                    environment=args.environment,
                    deployment=args.deployment,
                    play=args.play)

            send_hipchat_message(message)
    except Exception as e:
        message = 'An error occurred building AMI for {environment} ' \
            '{deployment} {play}.  The Exception was {exception}'.format(
                environment=args.environment,
                deployment=args.deployment,
                play=args.play,
                exception=repr(e))
        send_hipchat_message(message)
    finally:
        print
        if not args.no_cleanup and not args.noop:
            if sqs_queue:
                print "Cleaning up - Removing SQS queue - {}".format(run_id)
                sqs.delete_queue(sqs_queue)
            if instance_id:
                print "Cleaning up - Terminating instance ID - {}".format(
                    instance_id)
            # Check to make sure we have an instance id.
            if instance_id:
                ec2.terminate_instances(instance_ids=[instance_id])
