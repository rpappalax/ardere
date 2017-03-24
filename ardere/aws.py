"""AWS Helper Classes"""
import logging
import os
import time
import uuid
from collections import defaultdict

import boto3
import botocore
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List  # noqa

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Shell script to load
dir_path = os.path.dirname(os.path.realpath(__file__))
parent_dir_path = os.path.dirname(dir_path)
shell_path = os.path.join(parent_dir_path, "src", "shell",
                          "waitforcluster.sh")

# Load the shell script
with open(shell_path, 'r') as f:
    shell_script = f.read()

# List tracking vcpu's of all instance types for cpu unit reservations
# We are intentionally leaving out the following instance types as they're
# considered overkill for load-testing purposes or any instance req's we have
# experienced so far:
#     P2, G2, F1, I3, D2
ec2_type_by_vcpu = {
    1: ["t2.nano", "t2.micro", "t2.small", "m3.medium"],
    2: ["t2.medium", "t2.large", "m3.large", "m4.large", "c3.large",
        "c4.large", "r3.large", "r4.large"],
    4: ["t2.xlarge", "m3.xlarge", "m4.xlarge", "c3.xlarge", "c4.xlarge",
        "r3.xlarge", "r4.xlarge"],
    8: ["t2.2xlarge", "m3.2xlarge", "m4.2xlarge", "c3.2xlarge", "c4.2xlarge",
        "r3.2xlarge", "r4.2xlarge"],
    16: ["m4.4xlarge", "c3.4xlarge", "c4.4xlarge", "r3.4xlarge", "r4.4xlarge"],
    32: ["c3.8xlarge", "r3.8xlarge", "r4.8xlarge"],
    36: ["c4.8xlarge"],
    40: ["m4.10xlarge"],
    64: ["m4.16xlarge", "x1.16xlarge", "r4.16xlarge"],
    128: ["x1.32xlarge"]
}

# Build a list of vcpu's by instance type
ec2_vcpu_by_type = {}
for vcpu, instance_types in ec2_type_by_vcpu.items():
    for instance_type in instance_types:
        ec2_vcpu_by_type[instance_type] = vcpu


def cpu_units_for_instance_type(instance_type):
    """Calculate how many CPU units to allocate for an instance_type

    We calculate cpu_units as 1024 * vcpu's for each instance to allocate
    almost the entirety of the instance's cpu units to the load-testing
    container. We take out 512 to ensure some leftover capacity for other
    utility containers we run with the load-testing container.

    """
    return (ec2_vcpu_by_type[instance_type] * 1024) - 512


class ECSManager(object):
    """ECS Manager queries and manages an ECS cluster"""
    # For testing purposes
    boto = boto3

    # ECS optimized AMI id's
    ecs_ami_ids = {
        "us-east-1": "ami-b2df2ca4",
        "us-east-2": "ami-832b0ee6",
        "us-west-1": "ami-dd104dbd",
        "us-west-2": "ami-022b9262"
    }

    influxdb_container = "influxdb:1.1-alpine"

    def __init__(self, plan):
        # type: (Dict[str, Any]) -> None
        """Create and return a ECSManager for a cluster of the given name."""
        self._ecs_client = self.boto.client('ecs')
        self._ec2_client = self.boto.client('ec2')
        self._ecs_name = plan["ecs_name"]
        self._plan = plan

        # Pull out the env vars
        self.s3_ready_bucket = os.environ["s3_ready_bucket"]
        self.container_log_group = os.environ["container_log_group"]
        self.ecs_profile = os.environ["ecs_profile"]

        if "plan_run_uuid" not in plan:
            plan["plan_run_uuid"] = uuid.uuid4().hex

        self._plan_uuid = plan["plan_run_uuid"]

    @property
    def plan_uuid(self):
        return self._plan_uuid

    @property
    def s3_ready_file(self):
        return "https://s3.amazonaws.com/{bucket}/{key}".format(
            bucket=self.s3_ready_bucket,
            key="{}.ready".format(self._plan_uuid)
        )

    def family_name(self, step):
        """Generate a consistent family name for a given step"""
        return step["name"] + "-" + self._plan_uuid

    def metrics_family_name(self):
        return "{}-metrics".format(self._ecs_name)

    def query_active_instances(self):
        # type: () -> Dict[str, int]
        """Query EC2 for all the instances owned by ardere for this cluster."""
        instance_dict = defaultdict(int)
        paginator = self._ec2_client.get_paginator('describe_instances')
        response_iterator = paginator.paginate(
            Filters=[
                {
                    "Name": "tag:Owner",
                    "Values": ["ardere"]
                },
                {
                    "Name": "tag:ECSCluster",
                    "Values": [self._ecs_name]
                }
            ]
        )
        for page in response_iterator:
            for reservation in page["Reservations"]:
                for instance in reservation["Instances"]:
                    # Determine if the instance is pending/running and count
                    # 0 = Pending, 16 = Running, > is all shutting down, etc.
                    if instance["State"]["Code"] <= 16:
                        instance_dict[instance["InstanceType"]] += 1
        return instance_dict

    def calculate_missing_instances(self, desired, current):
        # type: (Dict[str, int], Dict[str, int]) -> Dict[str, int]
        """Determine how many of what instance types are needed to ensure
        the current instance dict has all the desired instance count/types."""
        needed = {}
        for instance_type, instance_count in desired.items():
            cur = current.get(instance_type, 0)
            if cur < instance_count:
                needed[instance_type] = instance_count - cur
        return needed

    def request_instances(self, instances):
        # type: (Dict[str, int]) -> None
        """Create requested types/quantities of instances for this cluster"""
        ami_id = self.ecs_ami_ids["us-east-1"]
        request_instances = []
        for instance_type, instance_count in instances.items():
            result = self._ec2_client.run_instances(
                ImageId=ami_id,
                KeyName="loads",
                MinCount=instance_count,
                MaxCount=instance_count,
                InstanceType=instance_type,
                UserData="#!/bin/bash \necho ECS_CLUSTER='" + self._ecs_name +
                         "' >> /etc/ecs/ecs.config",
                IamInstanceProfile={"Arn": self.ecs_profile}
            )

            # Track returned instances for tagging step
            request_instances.extend([x["InstanceId"] for x in
                                      result["Instances"]])

        self._ec2_client.create_tags(
            Resources=request_instances,
            Tags=[
                dict(Key="Owner", Value="ardere"),
                dict(Key="ECSCluster", Value=self._ecs_name)
            ]
        )

    def locate_metrics_container_ip(self):
        """Locates the metrics container IP"""
        response = self._ecs_client.list_container_instances(
            cluster=self._ecs_name,
            filter="task:group == service:metrics"
        )
        if not response["containerInstanceArns"]:
            return None

        container_arn = response["containerInstanceArns"][0]
        response = self._ecs_client.describe_container_instances(
            cluster=self._ecs_name,
            containerInstances=[container_arn]
        )

        ec2_instance_id = response["containerInstances"][0]["ec2InstanceId"]
        instance = self.boto.resource("ec2").Instance(ec2_instance_id)
        return instance.public_ip_address

    def locate_metrics_service(self):
        """Locate and return the metrics service arn if any"""
        response = self._ecs_client.describe_services(
            cluster=self._ecs_name,
            services=["metrics"]
        )
        if response["services"]:
            return response["services"][0]
        else:
            return None

    def create_influxdb_service(self, options):
        # type: (Dict[str, Any]) -> Dict[str, Any]
        """Creates an ECS service to run InfluxDB for metric reporting and
        returns its info"""
        logger.info("Creating InfluxDB service with options: {}".format(
            options))

        task_response = self._ecs_client.register_task_definition(
            family=self.metrics_family_name(),
            containerDefinitions=[
                {
                    "name": "metrics",
                    "image": self.influxdb_container,
                    "cpu": cpu_units_for_instance_type(
                        options["instance_type"]),
                    "memoryReservation": 256,
                    "portMappings": [
                        {"containerPort": 8086},
                        {"containerPort": 8088}
                    ],
                    "logConfiguration": {
                        "logDriver": "awslogs",
                        "options": {
                            "awslogs-group": self.container_log_group,
                            "awslogs-region": "us-east-1",
                            "awslogs-stream-prefix":
                                "ardere-{}".format(self.plan_uuid)
                        }
                    }
                }
            ],
            # use host network mode for optimal performance
            networkMode="host",

            placementConstraints=[
                # Ensure the service is confined to the right instance type
                {
                    "type": "memberOf",
                    "expression": "attribute:ecs.instance-type == {}".format(
                        options["instance_type"]),
                }
            ],
        )
        task_arn = task_response["taskDefinition"]["taskDefinitionArn"]
        service_result = self._ecs_client.create_service(
            cluster=self._ecs_name,
            serviceName="metrics",
            taskDefinition=task_arn,
            desiredCount=1,
            deploymentConfiguration={
                "minimumHealthyPercent": 0,
                "maximumPercent": 100
            },
            placementConstraints=[
                {
                    "type": "distinctInstance"
                }
            ]
        )
        service_arn = service_result["service"]["serviceArn"]
        return dict(task_arn=task_arn, service_arn=service_arn)

    def create_service(self, step):
        # type: (Dict[str, Any]) -> Dict[str, Any]
        """Creates an ECS service for a step and returns its info"""
        logger.info("CreateService called with: {}".format(step))

        # Prep the shell command
        wfc_var = '__ARDERE_WAITFORCLUSTER_SH__'
        wfc_cmd = 'sh -c "${}" waitforcluster.sh {} {}'.format(
            wfc_var,
            self.s3_ready_file,
            step.get("run_delay", 0)
        )
        service_cmd = step["cmd"]
        cmd = ['sh', '-c', '{} && {}'.format(wfc_cmd, service_cmd)]

        # Prep the env vars
        env_vars = [{"name": wfc_var, "value": shell_script}]
        for name, value in step.get("env", {}).items():
            env_vars.append({"name": name, "value": value})

        # ECS wants a family name for task definitions, no spaces, 255 chars
        family_name = step["name"] + "-" + self._plan_uuid

        # Use cpu_unit if provided, otherwise monopolize
        cpu_units = step.get("cpu_units",
                             cpu_units_for_instance_type(step["instance_type"]))

        # Setup the container definition
        container_def = {
            "name": step["name"],
            "image": step["container_name"],
            "cpu": cpu_units,

            # using only memoryReservation sets no hard limit
            "memoryReservation": 256,
            "environment": env_vars,
            "entryPoint": cmd,
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": self.container_log_group,
                    "awslogs-region": "us-east-1",
                    "awslogs-stream-prefix": "ardere-{}".format(
                        self.plan_uuid
                    )
                }
            }
        }

        if "port_mapping" in step:
            ports = [{"containerPort": port} for port in step["port_mapping"]]
            container_def["portMappings"] = ports

        task_response = self._ecs_client.register_task_definition(
            family=family_name,
            containerDefinitions=[
                container_def
            ],
            # use host network mode for optimal performance
            networkMode="host",

            placementConstraints=[
                # Ensure the service is confined to the right instance type
                {
                    "type": "memberOf",
                    "expression": "attribute:ecs.instance-type == {}".format(
                        step["instance_type"]),
                }
            ]
        )
        task_arn = task_response["taskDefinition"]["taskDefinitionArn"]
        step["taskArn"] = task_arn
        service_result = self._ecs_client.create_service(
            cluster=self._ecs_name,
            serviceName=step["name"],
            taskDefinition=task_arn,
            desiredCount=step["instance_count"],
            deploymentConfiguration={
                "minimumHealthyPercent": 0,
                "maximumPercent": 100
            },
            placementConstraints=[
                {
                    "type": "distinctInstance"
                }
            ]
        )
        step["serviceArn"] = service_result["service"]["serviceArn"]
        step["service_status"] = "STARTED"
        return step

    def create_services(self, steps):
        # type: (List[Dict[str, Any]]) -> None
        """Create ECS Services given a list of steps"""
        with ThreadPoolExecutor(max_workers=8) as executer:
            results = executer.map(self.create_service, steps)
        return list(results)

    def service_ready(self, step):
        # type: (Dict[str, Any]) -> bool
        """Query a service and return whether all its tasks are running"""
        service_name = step["name"]
        response = self._ecs_client.describe_services(
            cluster=self._ecs_name,
            services=[service_name]
        )

        try:
            deploy = response["services"][0]["deployments"][0]
        except (TypeError, IndexError):
            return False
        return deploy["desiredCount"] == deploy["runningCount"]

    def all_services_ready(self, steps):
        # type: (List[Dict[str, Any]]) -> bool
        """Queries all service ARN's in the plan to see if they're ready"""
        with ThreadPoolExecutor(max_workers=8) as executer:
            results = executer.map(self.service_ready, steps)
        return all(results)

    def stop_finished_service(self, start_time, step):
        # type: (start_time, Dict[str, Any]) -> None
        """Stops a service if it needs to shutdown"""
        if step["service_status"] == "STOPPED":
            return

        # Calculate time
        step_duration = step.get("run_delay", 0) + step["run_max_time"]
        now = time.time()
        if now < (start_time + step_duration):
            return

        # Running long enough to shutdown
        self._ecs_client.update_service(
            cluster=self._ecs_name,
            service=step["name"],
            desiredCount=0
        )
        step["service_status"] = "STOPPED"

    def stop_finished_services(self, start_time, steps):
        # type: (int, List[Dict[str, Any]]) -> None
        """Shuts down any services that have run for their max time"""
        for step in steps:
            self.stop_finished_service(start_time, step)

    def shutdown_plan(self, steps):
        """Terminate the entire plan, ensure all services and task
        definitions are completely cleaned up and removed"""
        # Locate all the services for the ECS Cluster
        paginator = self._ecs_client.get_paginator('list_services')
        response_iterator = paginator.paginate(
            cluster=self._ecs_name
        )

        # Collect all the service ARN's
        service_arns = []
        for page in response_iterator:
            service_arns.extend(page["serviceArns"])

        # Avoid shutting down metrics if tear down was not requested
        # We have to exclude it from the services discovered above if we
        # should NOT tear it down
        if not self._plan["influx_options"]["tear_down"]:
            metric_service = self.locate_metrics_service()
            if metric_service and metric_service["serviceArn"] in service_arns:
                service_arns.remove(metric_service["serviceArn"])

        for service_arn in service_arns:
            try:
                self._ecs_client.update_service(
                    cluster=self._ecs_name,
                    service=service_arn,
                    desiredCount=0
                )
            except botocore.exceptions.ClientError:
                continue

            try:
                self._ecs_client.delete_service(
                    cluster=self._ecs_name,
                    service=service_arn
                )
            except botocore.exceptions.ClientError:
                pass

        # Locate all the task definitions for this plan
        step_family_names = [self.family_name(step) for step in steps]

        # Add in the metrics family name if we need to tear_down
        if self._plan["influx_options"]["tear_down"]:
            step_family_names.append(self.metrics_family_name())

        for family_name in step_family_names:
            try:
                response = self._ecs_client.describe_task_definition(
                    taskDefinition=family_name
                )
            except botocore.exceptions.ClientError:
                continue

            task_arn = response["taskDefinition"]["taskDefinitionArn"]

            # Deregister the task
            try:
                self._ecs_client.deregister_task_definition(
                    taskDefinition=task_arn
                )
            except botocore.exceptions.ClientError:
                pass
