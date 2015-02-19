import click
from netaddr import IPNetwork
import time
import yaml

import aws_account_configurator
from aws_account_configurator.console import AliasedGroup, error, Action, info
import boto.cloudtrail
import boto.vpc


CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])

VPC_NET = IPNetwork('172.31.0.0/16')


def print_version(ctx, param, value):
    if not value or ctx.resilient_parsing:
        return
    click.echo('AWS Account Configurator {}'.format(aws_account_configurator.__version__))
    ctx.exit()


@click.group(cls=AliasedGroup, context_settings=CONTEXT_SETTINGS)
@click.option('-V', '--version', is_flag=True, callback=print_version, expose_value=False, is_eager=True)
def cli():
    pass


def find_vpc(conn):
    for vpc in conn.get_all_vpcs():
        if vpc.cidr_block == str(VPC_NET):
            return vpc


def find_subnet(subnets: list, name: str):
    for _subnet in subnets:
        if _subnet.tags.get('Name') == name:
            return _subnet


def configure_subnet(vpc_conn, vpc, az, _type: str, net: IPNetwork, subnets: list, dry_run: bool):
    name = '{}-{}'.format(_type, az.name)
    subnet = find_subnet(subnets, name)
    if not subnet:
        with Action('Creating subnet {name} with {net}..', **vars()):
            if not dry_run:
                subnet = vpc_conn.create_subnet(vpc.id, str(net), availability_zone=az.name)
                subnet.add_tags({'Name': name})


def calculate_subnet(vpc_net: IPNetwork, _type: str, az_index: int):
    '''
    >>> calculate_subnet(IPNetwork('10.0.0.0/16'), 'dmz', 0)
    IPNetwork('10.0.0.0/21')

    >>> calculate_subnet(IPNetwork('10.0.0.0/16'), 'internal', 0)
    IPNetwork('10.0.128.0/20')
    '''
    if _type == 'dmz':
        networks = list(vpc_net.subnet(21))
    else:
        # use the "upper half" of the /16 network for the internal/private subnets
        networks = list(list(vpc_net.subnet(vpc_net.prefixlen + 1))[1].subnet(20))
    return networks[az_index]


def find_trail(trails: list, name):
    for trail in trails:
        if trail.get('Name') == name:
            return trail


def configure_routing(ec2_conn, subnets: list, cfg: dict):
    for subnet in subnets:
        if subnet.tags.get('Name').startswith('dmz-'):

            sg_name = 'NAT-{}'.format(subnet.tags['Name'])
            sg = [group for group in ec2_conn.get_all_security_groups() if group.name == sg_name]
            if not sg:
                sg = ec2_conn.create_security_group(sg_name, 'NAT security group',
                                                    vpc_id=subnet.vpc_id)
                sg.add_tags({'Name': sg_name})

                for proto in 'tcp', 'udp':
                    sg.authorize(ip_protocol=proto,
                                 from_port=0,
                                 to_port=65535,
                                 cidr_ip=str(VPC_NET))
            else:
                sg = sg[0]

            images = ec2_conn.get_all_images(filters={'name': 'amzn-ami-vpc-nat-hvm*',
                                                      'owner_alias': 'amazon',
                                                      'root_device_type': 'ebs'})
            most_recent_image = sorted(images, key=lambda i: i.name)[-1]
            with Action('Launching NAT instance..') as act:
                res = ec2_conn.run_instances(most_recent_image.id, subnet_id=subnet.id,
                                             instance_type=cfg.get('instance_type', 'm3.medium'),
                                             security_group_ids=[sg.id])
                instance = res.instances[0]

                status = instance.update()
                while status == 'pending':
                    time.sleep(5)
                    status = instance.update()
                    act.progress()

                if status == 'running':
                    instance.add_tag('Name', 'NAT')


def configure_cloudtrail(account_name, region, cfg, dry_run):
    cloudtrail = boto.cloudtrail.connect_to_region(region)
    trails = cloudtrail.describe_trails()['trailList']
    name = '{}-{}'.format(account_name, region)
    trail = find_trail(trails, name)
    kwargs = dict(name=name,
                  s3_bucket_name=cfg['cloudtrail']['s3_bucket_name'],
                  s3_key_prefix=cfg['cloudtrail']['s3_key_prefix'],
                  include_global_service_events=True)
    if trail:
        with Action('Updating CloudTrail..'):
            if not dry_run:
                cloudtrail.update_trail(**kwargs)
    else:
        with Action('Enabling CloudTrail..'):
            if not dry_run:
                cloudtrail.create_trail(**kwargs)


@cli.command()
@click.argument('file', type=click.File('rb'))
@click.argument('account_name')
@click.option('--dry-run', is_flag=True)
def configure(file, account_name, dry_run):
    config = yaml.safe_load(file)
    cfg = config['accounts'].get(account_name)
    cfg.update(config.get('global', {}))
    regions = cfg['regions']

    if not cfg:
        error('No configuration found for account {}'.format(account_name))

    for region in regions:
        vpc_conn = boto.vpc.connect_to_region(region)
        ec2_conn = boto.ec2.connect_to_region(region)
        with Action('Checking region {region}..', **vars()):
            availability_zones = ec2_conn.get_all_zones()
        info('Availability zones: {}'.format(availability_zones))
        with Action('Finding VPC..'):
            vpc = find_vpc(vpc_conn)
        if not vpc:
            error('No default VPC found')
            with Action('Creating VPC for {cidr_block}..', cidr_block=str(VPC_NET)):
                if not dry_run:
                    vpc = vpc_conn.create_vpc(str(VPC_NET))
        with Action('Updating VPC name..'):
            if not dry_run:
                vpc.add_tags({'Name': '{}-{}'.format(account_name, region)})
        info(vpc)
        subnets = vpc_conn.get_all_subnets(filters={'vpcId': [vpc.id]})
        for subnet in subnets:
            if not subnet.tags.get('Name'):
                acls = vpc_conn.get_all_network_acls()
                for acl in acls:
                    for assoc in acl.associations:
                        print(assoc.__dict__)
                with Action('Deleting subnet {subnet_id}..', subnet_id=subnet.id):
                    if not dry_run:
                        vpc_conn.delete_subnet(subnet.id)
        for _type in 'dmz', 'internal':
            for i, az in enumerate(sorted(availability_zones, key=lambda az: az.name)):
                net = calculate_subnet(VPC_NET, _type, i)
                configure_subnet(vpc_conn, vpc, az, _type, net, subnets, dry_run)

        # All subnets now exist
        subnets = vpc_conn.get_all_subnets(filters={'vpcId': [vpc.id]})
        configure_routing(ec2_conn, subnets, cfg.get('nat', {}))
        configure_cloudtrail(account_name, region, cfg, dry_run)


def main():
    cli()
