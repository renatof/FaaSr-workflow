#!/usr/bin/env python3
import argparse
import logging
import os
import sys
from pathlib import Path

from FaaSr_py import FaaSrPayload, Scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)


def get_workflow_file():
    """Gets workflow file from command line arguments"""
    parser = argparse.ArgumentParser(
        description="Invoke a workflow defined in a JSON file"
    )
    parser.add_argument(
        "--workflow-file", required=True, help="Path to the workflow JSON file"
    )

    workflow_path = parser.parse_args().workflow_file

    if not Path(workflow_path).is_file():
        logger.error(f"Workflow file {workflow_path} not found")
        sys.exit(1)

    return workflow_path


def add_secrets_to_server_attributes(server, faas_type):
    """Adds secrets to compute server based on FaaS type"""

    match faas_type:
        case "GitHubActions":
            token = os.getenv("GH_PAT")
            if not token:
                logger.error(
                    "GH_PAT environment variable must be set for GitHub Action invocation"
                )
                sys.exit(1)
            server["Token"] = token

        case "Lambda":
            aws_access_key = os.getenv("AWS_AccessKey")
            aws_secret_key = os.getenv("AWS_SecretKey")

            if not aws_access_key or not aws_secret_key:
                logger.error(
                    "AWS_AccessKey and AWS_SecretKey environment variables must be set for Lambda invocation"  # noqa E501
                )
                sys.exit(1)

            server["AccessKey"] = aws_access_key
            server["SecretKey"] = aws_secret_key

        case "OpenWhisk":
            ow_api_key = os.getenv("OW_APIkey")
            if not ow_api_key:
                logger.error(
                    "OW_APIkey environment variable must be set for OpenWhisk invocation"
                )
                sys.exit(1)
            server["OW_APIkey"] = ow_api_key

        case "GoogleCloud":
            gcp_secret_key = os.getenv("GCP_SecretKey")
            if not gcp_secret_key:
                logger.error(
                    "GCP_SecretKey environment variable must be set for Google Cloud Functions invocation"  # noqa E501
                )
                sys.exit(1)
            server["SecretKey"] = gcp_secret_key

            token = os.getenv("GH_PAT")
            if not token:
                logger.warning(
                    "GH_PAT environment variable must be set for GitHub Action invocation"
                )
                sys.exit(1)
            server["Token"] = token

        case "SLURM":
            slurm_token = os.getenv("SLURM_Token")
            if not slurm_token:
                logger.error(
                    "SLURM_Token environment variable must be set for SLURM invocation"
                )
                sys.exit(1)
            server["SLURM_Token"] = slurm_token


def main(testing: bool = False) -> FaaSrPayload:
    """Function invocation script"""

    workflow_path = get_workflow_file()

    github_repo = os.getenv("GITHUB_REPOSITORY")
    ref = os.getenv("GITHUB_REF_NAME", "main")
    token = os.getenv("GH_PAT")

    file_path = f"{github_repo}/{ref}/{workflow_path}"

    if not token:
        logger.warning(
            "GH_PAT environment variable not set. Invocation will fail if repository is private"
        )

    try:
        workflow = FaaSrPayload(url=file_path, token=token)
    except Exception as e:
        logger.error(f"Exception raised while while initializing FaaSr payload: {e}")
        sys.exit(1)

    # If we are testing, we need to generate the invocation timestamp and id
    if testing:
        workflow._generate_invocation_timestamp()
        workflow._generate_invocation_id()

    workflow_name = workflow.get("WorkflowName")

    if not workflow_name:
        logger.error("WorkflowName not found in payload")
        sys.exit(1)

    entry_action_name = workflow.get("FunctionInvoke")

    if not entry_action_name:
        logger.error("FunctionInvoke not found in payload")
        sys.exit(1)

    try:
        server_name = workflow["ActionList"][entry_action_name]["FaaSServer"]

        server = workflow["ComputeServers"][server_name]

        faas_type = server["FaaSType"]

        use_secret_store = server.get("UseSecretStore", False)
    except KeyError as e:
        sys.exit(1)

    if not use_secret_store:
        logger.error("UseSecretStore must be true for initial action")
        sys.exit(1)

    # Add secret to entry action so that Scheduler can invoke it
    faas_type = server["FaaSType"]
    add_secrets_to_server_attributes(server, faas_type)

    try:
        faasr_scheduler = Scheduler(workflow)
        logger.info(f"Triggering entry action: {entry_action_name}")

        faasr_scheduler.trigger_func(workflow_name, entry_action_name)
    except Exception as e:
        logger.error(f"Trigger failed: {e}")
        sys.exit(1)

    return workflow


if __name__ == "__main__":
    main()
