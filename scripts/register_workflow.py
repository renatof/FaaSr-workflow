#!/usr/bin/env python3

import argparse
import json
import logging
import os
import subprocess
import sys
import textwrap
import time

import boto3
import requests
from FaaSr_py import graph_functions as faasr_gf
from github import Github

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Deploy FaaSr functions to specified platform"
    )
    parser.add_argument(
        "--workflow-file", required=True, help="Path to the workflow JSON file"
    )
    return parser.parse_args()


def read_workflow_file(file_path):
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"Error: Workflow file {file_path} not found")
        sys.exit(1)
    except json.JSONDecodeError:
        logger.error(f"Error: Invalid JSON in workflow file {file_path}")
        sys.exit(1)


def verify_containers(workflow_data):
    """Check if custom containers are specified via environment variable"""
    custom_container = os.getenv("CUSTOM_CONTAINER", "false").lower() == "true"

    if custom_container:
        logger.info("Using custom containers")
        return

    # Get set of native containers
    with open("scripts/native_containers.txt", "r") as f:
        native_containers = {line.strip() for line in f.readlines()}

    for container in workflow_data.get("ActionContainers", {}).values():
        if container not in native_containers:
            logger.error(
                f"Custom container {container} not in native_containers.txt -- to use it, you must enable custom containers" # noqa E501
            )
            sys.exit(1)


def generate_github_secret_imports(faasr_payload):
    """Generate GitHub Actions secret import commands from FaaSr payload."""
    import_statements = []

    # Add secrets for compute servers
    for faas_name, compute_server in faasr_payload.get("ComputeServers", {}).items():
        faas_type = compute_server.get("FaaSType", "")
        match (faas_type):
            case "GitHubActions":
                pat_secret = f"{faas_name}_PAT"
                import_statements.append(
                    f"{pat_secret}: ${{{{ secrets.{pat_secret}}}}}"
                )
            case "Lambda":
                access_key = f"{faas_name}_AccessKey"
                secret_key = f"{faas_name}_SecretKey"
                import_statements.extend(
                    [
                    f"{access_key}: ${{{{ secrets.{access_key}}}}}",
                    f"{secret_key}: ${{{{ secrets.{secret_key}}}}}",
                    ]
                )
            case "OpenWhisk":
                api_key = f"{faas_name}_APIkey"
                import_statements.append(f"{api_key}: ${{{{ secrets.{api_key}}}}}")
            case "GoogleCloud":
                secret_key = f"{faas_name}_SecretKey"
                import_statements.append(
                    f"{secret_key}: ${{{{ secrets.{secret_key}}}}}"
                )
            case "SLURM":
                token = f"{faas_name}_Token"
                import_statements.append(f"{token}: ${{{{ secrets.{token}}}}}")
            case _:
                logger.error(
                    f"Unknown FaaSType ({faas_type}) for compute server: {faas_name} - cannot generate secrets"  # noqa E501
                )
                sys.exit(1)

    # Add secrets for data stores
    for s3_name in faasr_payload.get("DataStores", {}).keys():
        secret_key = f"{s3_name}_SecretKey"
        access_key = f"{s3_name}_AccessKey"
        import_statements.extend(
            [
                f"{access_key}: ${{{{ secrets.{access_key}}}}}",
                f"{secret_key}: ${{{{ secrets.{secret_key}}}}}",
            ]
        )
    
    if "VMConfig" in faasr_payload:
        vm_config = faasr_payload["VMConfig"]
        vm_name = vm_config.get("Name")
        
        if vm_name:
            provider = vm_config.get("Provider", "AWS")
            
            if provider == "AWS":
                access_key = f"{vm_name}_AccessKey"
                secret_key = f"{vm_name}_SecretKey"
                import_statements.extend([
                    f"{access_key}: ${{{{ secrets.{access_key}}}}}",
                    f"{secret_key}: ${{{{ secrets.{secret_key}}}}}",
                ])

    # Indent each line for YAML formatting
    indent = " " * 20
    import_statements = "\n".join(f"{indent}{s}" for s in import_statements)

    return import_statements

def generate_serverless_yaml(action_name, container_image, secret_imports):
    """Generate YAML for serverless (GitHub-hosted runner)"""
    return textwrap.dedent(
        f"""\
        name: {action_name}

        on:
            workflow_dispatch:
                inputs:
                    OVERWRITTEN:
                        description: "Overwritten fields"
                        required: true
                    PAYLOAD_URL:
                        description: "URL to payload"
                        required: true

        jobs:
            run_docker_image:
                runs-on: ubuntu-latest
                container: {container_image}

                env:
{secret_imports}
                    OVERWRITTEN: ${{{{ github.event.inputs.OVERWRITTEN }}}}
                    PAYLOAD_URL: ${{{{ github.event.inputs.PAYLOAD_URL }}}}

                steps:
                  - name: Run Python entrypoint
                    run: |
                        cd /action
                        python3 faasr_entry.py
    """
    )


def generate_vm_yaml(action_name, container_image, secret_imports):
    """Generate YAML for VM (self-hosted runner)"""
    return textwrap.dedent(
        f"""\
        name: {action_name}

        on:
            workflow_dispatch:
                inputs:
                    OVERWRITTEN:
                        description: "Overwritten fields"
                        required: true
                    PAYLOAD_URL:
                        description: "URL to payload"
                        required: true

        jobs:
            run_on_vm:
                runs-on: self-hosted
                container: {container_image}

                env:
{secret_imports}
                    OVERWRITTEN: ${{{{ github.event.inputs.OVERWRITTEN }}}}
                    PAYLOAD_URL: ${{{{ github.event.inputs.PAYLOAD_URL }}}}

                steps:
                  - name: Run Python entrypoint
                    run: |
                        cd /action
                        python3 faasr_entry.py
    """
    )


def deploy_to_github(workflow_data):
    """Deploys GH functions to GitHub Actions"""
    github_token = os.getenv("GH_PAT")

    if not github_token:
        logger.error("GH_PAT environment variable not set")
        sys.exit(1)

    g = Github(github_token)

    # Get the workflow name for prefixing
    workflow_name = workflow_data.get("WorkflowName")

    # Ensure workflow name is specified
    if not workflow_name:
        logger.error("WorkflowName not specified in workflow file")
        sys.exit(1)

    json_prefix = workflow_name

    # Get the current repository
    repo_name = os.getenv("GITHUB_REPOSITORY")

    # Filter actions to be deployed to GitHub Actions
    github_actions = {}
    for action_name, action_data in workflow_data["ActionList"].items():
        server_name = action_data["FaaSServer"]
        server_config = workflow_data["ComputeServers"][server_name]
        faas_type = server_config["FaaSType"].lower()
        if faas_type == "githubactions":
            github_actions[action_name] = action_data

    if not github_actions:
        logger.info("No actions found for GitHub Actions deployment")
        return

    try:
        repo = g.get_repo(repo_name)

        # Get the default branch name
        default_branch = repo.default_branch
        logger.info(f"Using branch: {default_branch}")

        # Deploy each action
        for action_name, action_data in github_actions.items():
            # Create prefixed action name using workflow_name-action_name format
            prefixed_action_name = f"{json_prefix}-{action_name}"

            requires_vm = action_data.get("RequiresVM", False)

            # Create workflow file
            # Get container image, with fallback to default
            container_image = workflow_data.get("ActionContainers", {}).get(action_name)

            # Ensure container image is specified
            if not container_image:
                logger.error(f"No container specified for action: {action_name}")
                sys.exit(1)

            # Dynamically set required secrets and variables
            secret_imports = generate_github_secret_imports(workflow_data)

            if requires_vm:
                workflow_content = generate_vm_yaml(
                    prefixed_action_name,
                    container_image,
                    secret_imports
                )
            else:
                workflow_content = generate_serverless_yaml(
                    prefixed_action_name,
                    container_image,
                    secret_imports
                )

            # Create or update the workflow file
            workflow_path = f".github/workflows/{prefixed_action_name}.yml"
            try:
                # Try to get the file first
                contents = repo.get_contents(workflow_path)

                # Update YAML file
                repo.update_file(
                    path=workflow_path,
                    message=f"Update workflow for {prefixed_action_name}",
                    content=workflow_content,
                    sha=contents.sha,
                    branch=default_branch,
                )
                logger.info(f"Successfully updated {workflow_path}")
            except Exception as e:
                if "Not Found" in str(e) or "404" in str(e):
                    # If file doesn't exist, create it
                    logger.info(f"File {workflow_path} doesn't exist, creating...")
                    repo.create_file(
                        path=workflow_path,
                        message=f"Add workflow for {prefixed_action_name}",
                        content=workflow_content,
                        branch=default_branch,
                    )
                    logger.info(f"Successfully created {workflow_path}")
                else:
                    logger.info(f"Error updating/creating {workflow_path}: {str(e)}")
                    # Try to get more details about the error
                    if hasattr(e, "data"):
                        logger.error(f"Error details: {e.data}")
                    if hasattr(e, "status"):
                        logger.error(f"HTTP status: {e.status}")
                    raise e

            logger.info(f"Successfully deployed {prefixed_action_name} to GitHub")

    except Exception as e:
        logger.error(f"Error deploying to GitHub: {str(e)}")
        sys.exit(1)


def get_lambda_credentials(workflow_data):
    """Fetches AWS Lambda credentials from environment variables"""
    # Get AWS credentials
    aws_access_key, aws_secret_key = os.getenv("AWS_AccessKey"), os.getenv(
        "AWS_SecretKey"
    )

    # Fail if AWS creds not set
    if not aws_access_key or not aws_secret_key:
        logger.error(
            "AWS_AccessKey and AWS_SecretKey environment variables must be set"
        )
        sys.exit(1)

    aws_region = workflow_data.get("ComputeServers", {}).get("AWS", {}).get("Region")

    if not aws_region:
        logger.warning("AWS region not specified, defaulting to us-east-1")
        aws_region = "us-east-1"

    aws_arn = os.getenv("AWS_ARN")

    if not aws_arn:
        logger.error("AWS ARN not specified; AWS registration will fail")
        sys.exit(1)

    return (aws_access_key, aws_secret_key, aws_region, aws_arn)


def deploy_to_aws(workflow_data):
    """Deploys functions to AWS Lambda"""
    # Filter actions that should be deployed to AWS Lambda
    lambda_actions = {}
    for action_name, action_data in workflow_data["ActionList"].items():
        server_name = action_data["FaaSServer"]
        server_config = workflow_data["ComputeServers"][server_name]
        faas_type = server_config["FaaSType"].lower()
        if faas_type in ["lambda", "aws_lambda", "aws"]:
            lambda_actions[action_name] = action_data

    if not lambda_actions:
        logger.info("No actions found for AWS Lambda deployment")
        return

    # Get the workflow name to prepend to function names
    workflow_name = workflow_data.get("WorkflowName")

    if not workflow_name:
        logger.error("WorkflowName is not specified in workflow file")
        sys.exit(1)

    # Get AWS credentials
    aws_access_key, aws_secret_key, aws_region, aws_arn = get_lambda_credentials(workflow_data)

    lambda_client = boto3.client(
        "lambda",
        aws_access_key_id=aws_access_key,
        aws_secret_access_key=aws_secret_key,
        region_name=aws_region,
    )

    # Process each action in the workflow
    for action_name, action_data in lambda_actions.items():
        try:
            # Create prefixed function name using workflow_name-action_name format
            prefixed_func_name = f"{workflow_name}-{action_name}"

            # Get container image for AWS Lambda (must be an Amazon ECR image URI)
            container_image = workflow_data.get("ActionContainers", {}).get(action_name)
            if not container_image:
                logger.error(f"No container specified for action: {action_name}")
                sys.exit(1)

            # TODO: remove this
            # Check payload size before deployment
            # payload_size = len(workflow_data.encode("utf-8"))
            # if payload_size > 4000:  # Lambda env var limit is ~4KB
            #    logger.error(
            #        f"Warning: SECRET_PAYLOAD size ({payload_size} bytes) may exceed Lambda environment variable limits"
            #    )

            # Check if function already exists first
            try:
                lambda_client.get_function(FunctionName=prefixed_func_name)
                logger.info(
                    f"Function {prefixed_func_name} already exists, updating..."
                )
                # Update existing function
                lambda_client.update_function_code(
                    FunctionName=prefixed_func_name, ImageUri=container_image
                )

                # Wait for the function update to complete
                logger.info(
                    f"Waiting for {prefixed_func_name} code update to complete..."
                )
                max_attempts = 60  # Wait up to 5 minutes
                attempt = 0
                while attempt < max_attempts:
                    try:
                        response = lambda_client.get_function(
                            FunctionName=prefixed_func_name
                        )
                        state = response["Configuration"]["State"]
                        last_update_status = response["Configuration"][
                            "LastUpdateStatus"
                        ]

                        if state == "Active" and last_update_status == "Successful":
                            break
                        elif state == "Failed" or last_update_status == "Failed":
                            sys.exit(1)
                        else:
                            time.sleep(5)
                            attempt += 1
                    except Exception as e:
                        logger.info(f"Error checking function state: {str(e)}")
                        time.sleep(5)
                        attempt += 1

                if attempt >= max_attempts:
                    logger.error(
                        f"Timeout waiting for {prefixed_func_name} update to complete"
                    )
                    sys.exit(1)

                # Now update environment variables
                lambda_client.update_function_configuration(
                    FunctionName=prefixed_func_name,
                )
                logger.info(f"Successfully updated {prefixed_func_name} on AWS Lambda")

            except lambda_client.exceptions.ResourceNotFoundException:
                # Function doesn't exist, create it
                logger.info(f"Creating new Lambda function: {prefixed_func_name}")

                # TODO: is minimal function necessary here?
                try:
                    lambda_client.create_function(
                        FunctionName=prefixed_func_name,
                        PackageType="Image",
                        Code={"ImageUri": container_image},
                        Role=aws_arn,
                        Timeout=300,
                        MemorySize=128,
                    )

                    # Wait for the function to become active before updating
                    logger.info(f"Waiting for {prefixed_func_name} to become active...")
                    max_attempts = 120  # Wait up to 10 minutes
                    attempt = 0
                    while attempt < max_attempts:
                        try:
                            response = lambda_client.get_function(
                                FunctionName=prefixed_func_name
                            )
                            state = response["Configuration"]["State"]

                            if state == "Active":
                                logger.info(
                                    f"Function {prefixed_func_name} is now active"
                                )
                                break
                            elif state == "Failed":
                                logger.error(
                                    f"Function {prefixed_func_name} creation failed"
                                )
                                sys.exit(1)
                            else:
                                logger.info(f"Function state: {state}, waiting...")
                                time.sleep(5)
                                attempt += 1
                        except Exception as e:
                            logger.error(f"Error checking function state: {str(e)}")
                            time.sleep(5)
                            attempt += 1

                    if attempt >= max_attempts:
                        logger.error(
                            f"Timeout while waiting for {prefixed_func_name} to become active"
                        )
                        sys.exit(1)

                    # Now update with full configuration
                    # TODO: fetch timeout and memory size from workflow file
                    lambda_client.update_function_configuration(
                        FunctionName=prefixed_func_name,
                        Timeout=900,
                        MemorySize=1024,
                    )
                    logger.info(f"Updated {prefixed_func_name} with full configuration")

                except Exception as minimal_error:
                    logger.error(f"Minimal creation failed: {minimal_error}")
                    raise minimal_error
        except Exception as e:
            logger.error(f"Error deploying {prefixed_func_name} to AWS: {str(e)}")
            # logger.error additional debugging information
            if "RequestEntityTooLargeException" in str(e):
                logger.error(f"Payload too large - size: {len(workflow_data)} bytes")
                logger.error(
                    "Consider reducing workflow complexity or using external storage"
                )
            elif "InvalidParameterValueException" in str(e):
                logger.error(
                    "Check Lambda configuration parameters (memory, timeout, role)"
                )
            sys.exit(1)


def get_openwhisk_credentials(workflow_data):
    # Get OpenWhisk server configuration from workflow data
    for server_config in workflow_data["ComputeServers"].values():
        if server_config["FaaSType"].lower() == "openwhisk":
            return (
                server_config["Endpoint"],
                server_config["Namespace"],
            )

    logger.error("Error: No OpenWhisk server configuration found in workflow data")
    sys.exit(1)


def deploy_to_ow(workflow_data):
    # Get OpenWhisk credentials
    # TODO: AllowSelfSignedCertifcate
    api_host, namespace = get_openwhisk_credentials(workflow_data)

    # Get the workflow name for prefixing
    workflow_name = workflow_data.get("WorkflowName", "default")
    json_prefix = workflow_name

    # Filter actions that should be deployed to OpenWhisk
    ow_actions = {}
    for action_name, action_data in workflow_data["ActionList"].items():
        server_name = action_data["FaaSServer"]
        server_config = workflow_data["ComputeServers"][server_name]
        faas_type = server_config["FaaSType"].lower()
        if faas_type == "openwhisk":
            ow_actions[action_name] = action_data

    if not ow_actions:
        logger.info("No actions found for OpenWhisk deployment")
        return

    # Set up wsk properties
    subprocess.run(f"wsk property set --apihost {api_host}", shell=True)

    # Set authentication using API key from environment variable
    ow_api_key = os.getenv("OW_APIkey")
    if ow_api_key:
        subprocess.run(f"wsk property set --auth {ow_api_key}", shell=True)
        logger.info("Using OpenWhisk with API key authentication")
    else:
        logger.info("Using OpenWhisk without authentication")

    # Always use insecure flag to bypass certificate issues
    subprocess.run("wsk property set --insecure", shell=True)

    # TODO: why is this necessary?
    # Set environment variable to handle certificate issue
    env = os.environ.copy()
    env["GODEBUG"] = "x509ignoreCN=0"

    # Process each action in the workflow
    for action_name, action_data in ow_actions.items():
        try:
            # Create prefixed function name using workflow_name-action_name format
            prefixed_func_name = f"{json_prefix}-{action_name}"

            # Create or update OpenWhisk action using wsk CLI
            try:
                # First check if action exists (add --insecure flag)
                check_cmd = f"wsk action get {prefixed_func_name} --insecure >/dev/null 2>&1"
                exists = subprocess.run(check_cmd, shell=True, env=env).returncode == 0

                # Get container image, with fallback to default
                container_image = workflow_data.get("ActionContainers", {}).get(
                    action_name
                )

                if not container_image:
                    logger.error(f"No container specified for action: {action_name}")
                    sys.exit(1)

                if exists:
                    # Update existing action (add --insecure flag)
                    cmd = f"wsk action update {prefixed_func_name} --docker {container_image} --insecure"  # noqa E501
                else:
                    # Create new action (add --insecure flag)
                    cmd = f"wsk action create {prefixed_func_name} --docker {container_image} --insecure"  # noqa E501

                result = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, env=env
                )

                if result.returncode != 0:
                    raise Exception(
                        f"Failed to {'update' if exists else 'create'} action: {result.stderr}"
                    )

                logger.info(f"Successfully deployed {prefixed_func_name} to OpenWhisk")

            except Exception as e:
                logger.error(
                    f"Error deploying {prefixed_func_name} to OpenWhisk: {str(e)}"
                )
                sys.exit(1)

        except Exception as e:
            logger.error(f"Error processing {prefixed_func_name}: {str(e)}")
            sys.exit(1)

def get_gcp_resource_requirements(workflow_data, action_name, server_config):
    """
    Extract resource requirements with fallback hierarchy:
    Function-level → Server-level → Default values
    
    Args:
        workflow_data: Full workflow JSON
        action_name: Name of the action
        server_config: ComputeServers[server_name] config
        
    Returns:
        dict: Resource configuration for GCP
    """
    action_list = workflow_data.get("ActionList", {})
    action_config = action_list.get(action_name, {})
    
    function_resources = action_config.get("Resources", {})
    
    max_memory = action_config.get("MaxMemory")
    max_runtime = action_config.get("MaxRuntime")
    
    config = {
        "cpu": str(
            function_resources.get("CPUsPerTask")
            or server_config.get("CPUsPerTask")
            or 1
        ),
        "memory_mb": (
            function_resources.get("Memory")
            or max_memory
            or server_config.get("Memory")
            or 512
        ),
        "timeout_seconds": (
            function_resources.get("TimeLimit")
            or max_runtime
            or server_config.get("TimeLimit")
            or 3600
        )
    }
    
    return config

def create_gcp_job_definition(container_image,service_account, resources):
    """
    Creates a Cloud Run Job definition following GCP's API v2 schema.
    
    Args:
        container_image: Container image URL
        service_account: Service account email (REQUIRED)
        resources: Dict with cpu, memory_mb, timeout_seconds
    """
    return {
        "template": {
            "template": {
                "containers": [
                    {
                        "image": container_image,
                        "resources": {
                            "limits": {
                                "cpu": resources["cpu"],
                                "memory": f"{resources['memory_mb']}Mi"
                            }
                        }
                    }
                ],
                "timeout": f"{resources['timeout_seconds']}s",
                "serviceAccount": service_account
            }
        }
    }


def deploy_to_gcp(workflow_data):
    
    gcp_secret_key = os.getenv("GCP_SecretKey")
    
    if not gcp_secret_key:
        logger.error("GCP_SecretKey environment variable not set")
        sys.exit(1)
    
    from FaaSr_py.helpers.gcp_auth import refresh_gcp_access_token
    
    workflow_name = workflow_data.get("WorkflowName")
    
    if not workflow_name:
        logger.error("WorkflowName not specified in workflow file")
        sys.exit(1)
    
    gcp_actions = {}
    gcp_server_config = None
    gcp_server_name = None
    
    for action_name, action_data in workflow_data["ActionList"].items():
        server_name = action_data["FaaSServer"]
        server_config = workflow_data["ComputeServers"][server_name]
        faas_type = server_config.get("FaaSType", "")
        
        if faas_type.lower() == "googlecloud":
            gcp_actions[action_name] = action_data
            if not gcp_server_config:
                gcp_server_config = server_config.copy()
                gcp_server_name = server_name
    
    if not gcp_actions:
        logger.info("No actions found for GCP deployment")
        return
    
    gcp_server_config["SecretKey"] = gcp_secret_key
    
    temp_payload = {
        "ComputeServers": {
            gcp_server_name: gcp_server_config
        }
    }
    
    try:
        access_token = refresh_gcp_access_token(temp_payload, gcp_server_name)
        logger.info("Successfully authenticated with GCP")
    except Exception as e:
        logger.error(f"Failed to authenticate with GCP: {e}")
        sys.exit(1)
    
    endpoint = gcp_server_config.get("Endpoint", "run.googleapis.com/v2/projects/")
    namespace = gcp_server_config["Namespace"]
    region = gcp_server_config["Region"]
    
    if not endpoint.startswith("https://"):
        endpoint = f"https://{endpoint}"
    
    base_url = f"{endpoint}{namespace}/locations/{region}/jobs"
    
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}"
    }
    
    for action_name, action_data in gcp_actions.items():
        job_name = f"{workflow_name}-{action_name}"
        
        logger.info(f"Registering GCP Cloud Run Job: {job_name}")
        
        container_image = workflow_data.get("ActionContainers", {}).get(action_name)
        
        if not container_image:
            logger.error(f"No container specified for action: {action_name}")
            sys.exit(1)

        service_account = gcp_server_config.get("ClientEmail")
        if not service_account:
            logger.error(
                f"ClientEmail (service account) is required for GoogleCloud server "
                f"but not found in ComputeServers configuration"
            )
            sys.exit(1)

        resources = get_gcp_resource_requirements(
            workflow_data=workflow_data,
            action_name=action_name,
            server_config=gcp_server_config
        )
        
        job_body = create_gcp_job_definition(
            container_image=container_image,
            service_account=service_account,
            resources=resources
        )
        
        create_url = base_url
        create_params = {"jobId": job_name}
        
        response = requests.post(
            create_url,
            json=job_body,
            headers=headers,
            params=create_params
        )
        
        if response.status_code in [200, 201]:
            logger.info(f"Successfully created Cloud Run Job: {job_name}")
        elif response.status_code == 409:
            logger.info(f"Job {job_name} already exists, updating...")
            update_url = f"{base_url}/{job_name}"
            
            response = requests.patch(
                update_url,
                json=job_body,
                headers=headers
            )
            
            if response.status_code in [200, 201]:
                logger.info(f"Successfully updated Cloud Run Job: {job_name}")
            else:
                logger.error(f"Failed to update job {job_name}: {response.text}")
                sys.exit(1)
        else:
            logger.error(f"Failed to create job {job_name}: {response.text}")
            sys.exit(1)
    
    logger.info(f"Successfully registered {len(gcp_actions)} GCP Cloud Run Jobs")

def deploy_to_slurm(workflow_data):
    """
    Validate SLURM configuration and test connectivity.
    This function validates configuration and tests connectivity.
    
    Args:
        workflow_data: Full workflow JSON
    """
    logger.info("Validating SLURM configuration...")
    
    # Find all SLURM actions
    slurm_actions = {}
    slurm_servers = {}
    
    for action_name, action_data in workflow_data["ActionList"].items():
        server_name = action_data["FaaSServer"]
        server_config = workflow_data["ComputeServers"][server_name]
        faas_type = server_config.get("FaaSType", "")
        
        if faas_type == "SLURM":
            if server_name not in slurm_actions:
                slurm_actions[server_name] = []
                slurm_servers[server_name] = server_config.copy()
            slurm_actions[server_name].append(action_name)
    
    if not slurm_actions:
        logger.info("No actions found for SLURM deployment")
        return
    
    # Process each SLURM server
    for server_name, actions in slurm_actions.items():
        logger.info(f"Registering workflow for SLURM: {server_name}")
        server_config = slurm_servers[server_name]
        
        # Validate server configuration
        validate_slurm_server_config(server_name, server_config)
        
        # Test connectivity
        if not test_slurm_connectivity(server_name, server_config):
            logger.error(f"Failed to connect to SLURM server: {server_name}")
            sys.exit(1)
        
        # Validate each action
        for action_name in actions:
            validate_slurm_action(action_name, workflow_data, server_config)
        
        logger.info(
            f"Successfully validated {len(actions)} action(s) for SLURM server '{server_name}'"
        )
    
    logger.info(
        f"SLURM configuration validated successfully. "
        f"No persistent resources created - jobs will be submitted at invocation time."
    )


def validate_slurm_server_config(server_name, server_config):
    """
    Validate SLURM server configuration has required fields.
    
    Args:
        server_name: Name of the SLURM server
        server_config: Server configuration dict
    """
    required_fields = ["Endpoint", "APIVersion", "Partition", "UserName"]
    missing_fields = [f for f in required_fields if not server_config.get(f)]
    
    if missing_fields:
        logger.error(
            f"SLURM server '{server_name}' configuration missing required fields: "
            f"{', '.join(missing_fields)}"
        )
        sys.exit(1)
    
    logger.info(
        f"SLURM server configuration validated: "
        f"{server_config['Endpoint']} (API: {server_config['APIVersion']})"
    )


def test_slurm_connectivity(server_name, server_config):
    """
    Test connectivity to SLURM REST API endpoint (mirrors R implementation).
    
    Args:
        server_name: Name of the SLURM server
        server_config: Server configuration dict
        
    Returns:
        bool: True if connectivity test passes
    """
    endpoint = server_config["Endpoint"]
    api_version = server_config.get("APIVersion", "v0.0.37")
    
    # Ensure endpoint has protocol
    if not endpoint.startswith("http"):
        endpoint = f"http://{endpoint}"
    
    # Test ping endpoint
    ping_url = f"{endpoint}/slurm/{api_version}/ping"
    
    # Prepare headers
    headers = {"Accept": "application/json"}
    
    # Add JWT token and username if available (for auth testing)
    slurm_token = os.getenv("SLURM_Token")
    if slurm_token:
        headers["X-SLURM-USER-TOKEN"] = slurm_token
        username = server_config.get("UserName", "ubuntu")
        headers["X-SLURM-USER-NAME"] = username
        
        # Validate token format
        if not slurm_token.startswith("eyJ"):
            logger.warning(
                f"SLURM_Token for '{server_name}' doesn't appear to be a valid JWT token"
            )
    
    try:
        response = requests.get(ping_url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            logger.info(f"✓ SLURM connectivity test passed for: {server_name}")
            return True
        elif response.status_code in [401, 403]:
            # Authentication required but endpoint is reachable
            logger.info(
                f"SLURM endpoint reachable at: {server_name} "
            )
            return True
        else:
            logger.error(
                f"SLURM connectivity test failed: HTTP {response.status_code} - "
                f"{response.text[:200]}"
            )
            return False
            
    except requests.exceptions.RequestException as e:
        logger.error(f"SLURM connectivity error for '{server_name}': {e}")
        return False


def validate_slurm_action(action_name, workflow_data, server_config):
    """
    Validate a single SLURM action configuration.
    
    Args:
        action_name: Name of the action
        workflow_data: Full workflow JSON
        server_config: Server configuration dict
    """
    action_config = workflow_data["ActionList"][action_name]
    
    # Validate container image
    container_image = workflow_data.get("ActionContainers", {}).get(action_name)
    if not container_image:
        logger.error(f"No container specified for SLURM action: {action_name}")
        sys.exit(1)
    
    # Get resource requirements using fallback hierarchy
    resources = get_slurm_resource_requirements(
        action_name, action_config, server_config
    )
    
    logger.info(
        f"Validated action '{action_name}': "
        f"container={container_image}, "
        f"resources=[CPU:{resources['cpus_per_task']}, "
        f"Memory:{resources['memory_mb']}MB, "
        f"Time:{resources['time_limit']}s]"
    )


def get_slurm_resource_requirements(action_name, action_config, server_config):
    """
    Extract SLURM resource requirements with fallback hierarchy.
    Function-level → Server-level → Default values
    
    Args:
        action_name: Name of the action
        action_config: Action configuration dict
        server_config: Server configuration dict
        
    Returns:
        dict: Resource configuration
    """
    # Function-level resources (highest priority)
    function_resources = action_config.get("Resources", {})
    
    # Extract with fallback hierarchy
    config = {
        "partition": (
            function_resources.get("Partition")
            or server_config.get("Partition")
            or "faasr"
        ),
        "nodes": (
            function_resources.get("Nodes") 
            or server_config.get("Nodes") 
            or 1
        ),
        "tasks": (
            function_resources.get("Tasks") 
            or server_config.get("Tasks") 
            or 1
        ),
        "cpus_per_task": (
            function_resources.get("CPUsPerTask")
            or server_config.get("CPUsPerTask")
            or 1
        ),
        "memory_mb": (
            function_resources.get("Memory")
            or server_config.get("Memory")
            or 1024
        ),
        "time_limit": (
            function_resources.get("TimeLimit")
            or server_config.get("TimeLimit")
            or 60
        ),
        "working_dir": (
            function_resources.get("WorkingDirectory")
            or server_config.get("WorkingDirectory")
            or "/tmp"
        ),
    }
    
    return config


def main():
    args = parse_arguments()
    workflow_data = read_workflow_file(args.workflow_file)

    # Store the workflow file path in the workflow data
    workflow_data["_workflow_file"] = args.workflow_file

    # Validate workflow for cycles and unreachable states
    logger.info("Validating workflow for cycles and unreachable states...")
    try:
        faasr_gf.check_dag(workflow_data)
        logger.info("Workflow validation passed")
    except SystemExit:
        logger.info("Workflow validation failed - check logs for details")

    # Verify if custom containers are specified correctly
    verify_containers(workflow_data)

    # Get all unique FaaSTypes from workflow data
    faas_types = set()
    for server in workflow_data.get("ComputeServers", {}).values():
        if "FaaSType" in server:
            faas_types.add(server["FaaSType"].lower())

    if not faas_types:
        logger.error("Error: No FaaSType found in workflow file")
        sys.exit(1)

    logger.info(f"Found FaaS platforms: {', '.join(faas_types)}")

    # Deploy to each platform found
    for faas_type in faas_types:
        logger.info(f"\nDeploying to {faas_type}...")
        if faas_type == "lambda":
            deploy_to_aws(workflow_data)
        elif faas_type == "githubactions":
            deploy_to_github(workflow_data)
        elif faas_type == "openwhisk":
            deploy_to_ow(workflow_data)
        elif faas_type == "googlecloud":
            deploy_to_gcp(workflow_data)
        elif faas_type == "slurm":
            deploy_to_slurm(workflow_data)
        else:
            logger.error(f"Unsupported FaaSType: {faas_type}")
            sys.exit(1)


if __name__ == "__main__":
    main()
