import logging
from string import Template
from typing import Any
from typing import Dict
from typing import List

import boto3.session
import neo4j

from .util import get_botocore_config
from cartography.intel.aws.util import AwsGraphJobParameters
from cartography.intel.aws.util import AwsStageConfig
from cartography.util import aws_handle_regions
from cartography.util import run_cleanup_job
from cartography.util import timeit

logger = logging.getLogger(__name__)


@timeit
@aws_handle_regions
def get_ec2_vpcs(boto3_session: boto3.session.Session, region: str) -> List[Dict[str, Any]]:
    client = boto3_session.client('ec2', region_name=region, config=get_botocore_config())
    return client.describe_vpcs()['Vpcs']


def _get_cidr_association_statement(block_type: str) -> str:
    INGEST_CIDR_TEMPLATE = Template("""
    MATCH (vpc:AWSVpc{id: {VpcId}})
    WITH vpc
    UNWIND {CidrBlock} as block_data
        MERGE (new_block:$block_label{id: {VpcId} + '|' + block_data.$block_cidr})
        ON CREATE SET new_block.firstseen = timestamp()
        SET new_block.association_id = block_data.AssociationId,
        new_block.cidr_block = block_data.$block_cidr,
        new_block.block_state = block_data.$state_name.State,
        new_block.block_state_message = block_data.$state_name.StatusMessage,
        new_block.lastupdated = {aws_update_tag}
        WITH vpc, new_block
        MERGE (vpc)-[r:BLOCK_ASSOCIATION]->(new_block)
        ON CREATE SET r.firstseen = timestamp()
        SET r.lastupdated = {aws_update_tag}""")

    BLOCK_CIDR = "CidrBlock"
    STATE_NAME = "CidrBlockState"

    # base label type. We add the AWS ipv4 or 6 depending on block type
    BLOCK_TYPE = "AWSCidrBlock"

    if block_type == "ipv6":
        BLOCK_CIDR = "Ipv6" + BLOCK_CIDR
        STATE_NAME = "Ipv6" + STATE_NAME
        BLOCK_TYPE = BLOCK_TYPE + ":AWSIpv6CidrBlock"
    elif block_type == "ipv4":
        BLOCK_TYPE = BLOCK_TYPE + ":AWSIpv4CidrBlock"
    else:
        raise ValueError(f"Unsupported block type specified - {block_type}")

    return INGEST_CIDR_TEMPLATE.safe_substitute(block_label=BLOCK_TYPE, block_cidr=BLOCK_CIDR, state_name=STATE_NAME)


@timeit
def load_cidr_association_set(
    neo4j_session: neo4j.Session, vpc_id: str, vpc_data: Dict[str, Any], block_type: str, aws_update_tag: int,
) -> None:
    ingest_statement = _get_cidr_association_statement(block_type)

    if block_type == "ipv6":
        data = vpc_data.get("Ipv6CidrBlockAssociationSet", [])
    else:
        data = vpc_data.get("CidrBlockAssociationSet", [])

    neo4j_session.run(
        ingest_statement,
        VpcId=vpc_id,
        CidrBlock=data,
        aws_update_tag=aws_update_tag,
    )


@timeit
def load_ec2_vpcs(
    neo4j_session: neo4j.Session, data: List[Dict[str, Any]], region: str, current_aws_account_id: str,
    aws_update_tag: int,
) -> None:
    # https://docs.aws.amazon.com/cli/latest/reference/ec2/describe-vpcs.html
    # {
    #     "Vpcs": [
    #         {
    #             "VpcId": "vpc-a01106c2",
    #             "InstanceTenancy": "default",
    #             "Tags": [
    #                 {
    #                     "Value": "MyVPC",
    #                     "Key": "Name"
    #                 }
    #             ],
    #             "CidrBlockAssociations": [
    #                 {
    #                     "AssociationId": "vpc-cidr-assoc-a26a41ca",
    #                     "CidrBlock": "10.0.0.0/16",
    #                     "CidrBlockState": {
    #                         "State": "associated"
    #                     }
    #                 }
    #             ],
    #             "State": "available",
    #             "DhcpOptionsId": "dopt-7a8b9c2d",
    #             "CidrBlock": "10.0.0.0/16",
    #             "IsDefault": false
    #         }
    #     ]
    # }

    ingest_vpc = """
    MERGE (new_vpc:AWSVpc{id: {VpcId}})
    ON CREATE SET new_vpc.firstseen = timestamp(), new_vpc.vpcid ={VpcId}
    SET new_vpc.instance_tenancy = {InstanceTenancy},
    new_vpc.state = {State},
    new_vpc.is_default = {IsDefault},
    new_vpc.primary_cidr_block = {PrimaryCIDRBlock},
    new_vpc.dhcp_options_id = {DhcpOptionsId},
    new_vpc.region = {Region},
    new_vpc.lastupdated = {aws_update_tag}
    WITH new_vpc
    MATCH (awsAccount:AWSAccount{id: {AWS_ACCOUNT_ID}})
    MERGE (awsAccount)-[r:RESOURCE]->(new_vpc)
    ON CREATE SET r.firstseen = timestamp()
    SET r.lastupdated = {aws_update_tag}"""

    for vpc in data:
        vpc_id = vpc["VpcId"]  # fail if not present

        neo4j_session.run(
            ingest_vpc,
            VpcId=vpc_id,
            InstanceTenancy=vpc.get("InstanceTenancy", None),
            State=vpc.get("State", None),
            IsDefault=vpc.get("IsDefault", None),
            PrimaryCIDRBlock=vpc.get("CidrBlock", None),
            DhcpOptionsId=vpc.get("DhcpOptionsId", None),
            Region=region,
            AWS_ACCOUNT_ID=current_aws_account_id,
            aws_update_tag=aws_update_tag,
        )

        load_cidr_association_set(
            neo4j_session,
            vpc_id=vpc_id,
            block_type="ipv4",
            vpc_data=vpc,
            aws_update_tag=aws_update_tag,
        )

        load_cidr_association_set(
            neo4j_session,
            vpc_id=vpc_id,
            block_type="ipv6",
            vpc_data=vpc,
            aws_update_tag=aws_update_tag,
        )


@timeit
def cleanup_ec2_vpcs(neo4j_session: neo4j.Session, graph_job_parameters: AwsGraphJobParameters) -> None:
    run_cleanup_job('aws_import_vpc_cleanup.json', neo4j_session, graph_job_parameters)


@timeit
def sync_vpc(neo4j_session: neo4j.Session, aws_stage_config: AwsStageConfig) -> None:
    for region in aws_stage_config.current_aws_account_regions:
        logger.info("Syncing EC2 VPC for region '%s' in account '%s'.", region, aws_stage_config.current_aws_account_id)
        data = get_ec2_vpcs(aws_stage_config.boto3_session, region)
        load_ec2_vpcs(
            neo4j_session,
            data,
            region,
            aws_stage_config.current_aws_account_id, aws_stage_config.graph_job_parameters['UPDATE_TAG'],
        )
    cleanup_ec2_vpcs(neo4j_session, aws_stage_config.graph_job_parameters)
