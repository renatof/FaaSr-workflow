#!/usr/bin/env python3

import argparse
import json
import logging
import os
import re
import subprocess
import sys

import boto3
import requests
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
        description="Set or unset a cron timer for a FaaSr workflow action"
    )
    parser.add_argument(
        "--workflow-file", required=True, help="Path to the workflow JSON file"
    )
    parser.add_argument(
        "--cron",
        default="",
        help="Cron expression (e.g. '*/5 * * * *'). Ignored when --unset is set.",
    )
    parser.add_argument(
        "--target",
        default="",
        help="Action name to schedule. Defaults to workflow's FunctionInvoke.",
    )
    parser.add_argument(
        "--unset",
        action="store_true",
        help="Unset (remove) the timer for the target action instead of setting it",
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


def resolve_target(workflow_data, target):
    if target:
        bare = re.split(r"[()]", target)[0]
    else:
        entry = workflow_data.get("FunctionInvoke")
        if not entry:
            logger.error("No --target provided and FunctionInvoke missing in workflow")
            sys.exit(1)
        bare = re.split(r"[()]", entry)[0]

    if bare not in workflow_data.get("ActionList", {}):
        logger.error(f"Target action '{bare}' not found in ActionList")
        sys.exit(1)
    return bare


def get_faas_type(workflow_data, target):
    server_name = workflow_data["ActionList"][target]["FaaSServer"]
    return workflow_data["ComputeServers"][server_name]["FaaSType"], server_name


# GitHub Actions 


def set_timer_github(workflow_data, target, cron, unset, workflow_file):
    """Create/delete a wrapper workflow that runs FAASR INVOKE on a schedule
    """
    if target != workflow_data.get("FunctionInvoke", target):
        logger.warning(
            "--target is ignored for GitHubActions timer; the wrapper always "
            "starts at the workflow's FunctionInvoke"
        )

    github_token = os.getenv("GH_PAT")
    if not github_token:
        logger.error("GH_PAT environment variable not set")
        sys.exit(1)

    workflow_name = workflow_data.get("WorkflowName")
    if not workflow_name:
        logger.error("WorkflowName not specified in workflow file")
        sys.exit(1)

    repo_name = os.getenv("GITHUB_REPOSITORY")
    if not repo_name:
        logger.error("GITHUB_REPOSITORY environment variable not set")
        sys.exit(1)

    g = Github(github_token)
    repo = g.get_repo(repo_name)
    default_branch = repo.default_branch

    timer_file_name = f"{workflow_name}-timer.yml"
    timer_path = f".github/workflows/{timer_file_name}"

    try:
        existing = repo.get_contents(timer_path, ref=default_branch)
    except Exception:
        existing = None

    if unset:
        if existing is None:
            logger.info(f"No timer wrapper at {timer_path} to remove")
            return
        repo.delete_file(
            path=timer_path,
            message=f"Unset timer for {workflow_name}",
            sha=existing.sha,
            branch=default_branch,
        )
        logger.info(f"Removed timer wrapper {timer_path}")
        return

    content = _generate_timer_wrapper_yaml(workflow_name, cron, workflow_file)

    if existing is None:
        repo.create_file(
            path=timer_path,
            message=f"Set timer ({cron}) for {workflow_name}",
            content=content,
            branch=default_branch,
        )
        logger.info(f"Created timer wrapper {timer_path} with cron '{cron}'")
    else:
        if existing.decoded_content.decode("utf-8") == content:
            logger.info(f"Timer wrapper {timer_path} already at cron '{cron}'")
            return
        repo.update_file(
            path=timer_path,
            message=f"Update timer ({cron}) for {workflow_name}",
            content=content,
            sha=existing.sha,
            branch=default_branch,
        )
        logger.info(f"Updated timer wrapper {timer_path} with cron '{cron}'")


def _generate_timer_wrapper_yaml(workflow_name, cron, workflow_file):
    return (
        f"name: ' ({workflow_name} TIMER)'\n"
        f"\n"
        f"on:\n"
        f"  schedule:\n"
        f"    - cron: \"{cron}\"\n"
        f"  workflow_dispatch:\n"
        f"\n"
        f"jobs:\n"
        f"  trigger:\n"
        f"    runs-on: ubuntu-latest\n"
        f"    steps:\n"
        f"      - name: Checkout repository\n"
        f"        uses: actions/checkout@v3\n"
        f"\n"
        f"      - name: Set up Python\n"
        f"        uses: actions/setup-python@v4\n"
        f"        with:\n"
        f"          python-version: '3.10'\n"
        f"\n"
        f"      - name: Install dependencies\n"
        f"        run: |\n"
        f"          python -m pip install --upgrade pip\n"
        f"          pip install boto3 requests jsonschema cryptography FaaSr_py\n"
        f"\n"
        f"      - name: Install OpenWhisk CLI\n"
        f"        run: |\n"
        f"          wget -q https://github.com/apache/openwhisk-cli/releases/download/1.2.0/OpenWhisk_CLI-1.2.0-linux-amd64.tgz\n"  # noqa E501
        f"          tar -xzf OpenWhisk_CLI-1.2.0-linux-amd64.tgz\n"
        f"          sudo mv wsk /usr/local/bin/wsk\n"
        f"          sudo chmod +x /usr/local/bin/wsk\n"
        f"\n"
        f"      - name: Trigger function\n"
        f"        env:\n"
        f"          OW_APIkey: ${{{{ secrets.OW_APIkey }}}}\n"
        f"          AWS_AccessKey: ${{{{ secrets.AWS_AccessKey }}}}\n"
        f"          AWS_SecretKey: ${{{{ secrets.AWS_SecretKey }}}}\n"
        f"          GCP_SecretKey: ${{{{ secrets.GCP_SecretKey }}}}\n"
        f"          SLURM_Token: ${{{{ secrets.SLURM_Token }}}}\n"
        f"          GH_PAT: ${{{{ secrets.GH_PAT }}}}\n"
        f"        run: |\n"
        f"          echo \"GODEBUG=x509ignoreCN=0\" >> $GITHUB_ENV\n"
        f"          python scripts/invoke_workflow.py --workflow-file {workflow_file}\n"
    )


# AWS Lambda


def set_timer_lambda(workflow_data, target, cron, unset):
    aws_access_key = os.getenv("AWS_AccessKey")
    aws_secret_key = os.getenv("AWS_SecretKey")
    if not aws_access_key or not aws_secret_key:
        logger.error("AWS_AccessKey and AWS_SecretKey environment variables must be set")
        sys.exit(1)

    workflow_name = workflow_data.get("WorkflowName")
    if not workflow_name:
        logger.error("WorkflowName not specified in workflow file")
        sys.exit(1)

    _, server_name = get_faas_type(workflow_data, target)
    aws_region = (
        workflow_data["ComputeServers"][server_name].get("Region") or "us-east-1"
    )

    function_name = f"{workflow_name}-{target}"
    rule_name = f"{function_name}-timer"

    events = boto3.client(
        "events",
        aws_access_key_id=aws_access_key,
        aws_secret_access_key=aws_secret_key,
        region_name=aws_region,
    )
    lambda_client = boto3.client(
        "lambda",
        aws_access_key_id=aws_access_key,
        aws_secret_access_key=aws_secret_key,
        region_name=aws_region,
    )

    if unset:
        try:
            events.remove_targets(Rule=rule_name, Ids=["1"])
        except events.exceptions.ResourceNotFoundException:
            pass
        try:
            events.delete_rule(Name=rule_name)
            logger.info(f"Removed EventBridge rule {rule_name}")
        except events.exceptions.ResourceNotFoundException:
            logger.info(f"EventBridge rule {rule_name} did not exist")
        try:
            lambda_client.remove_permission(
                FunctionName=function_name, StatementId=f"{rule_name}-invoke"
            )
        except lambda_client.exceptions.ResourceNotFoundException:
            pass
        return

    schedule_expr = f"cron({_aws_cron(cron)})"
    events.put_rule(
        Name=rule_name,
        ScheduleExpression=schedule_expr,
        State="ENABLED",
        Description=f"FaaSr timer for {function_name}",
    )

    func = lambda_client.get_function(FunctionName=function_name)
    func_arn = func["Configuration"]["FunctionArn"]
    rule_arn = events.describe_rule(Name=rule_name)["Arn"]

    try:
        lambda_client.add_permission(
            FunctionName=function_name,
            StatementId=f"{rule_name}-invoke",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=rule_arn,
        )
    except lambda_client.exceptions.ResourceConflictException:
        pass

    events.put_targets(
        Rule=rule_name,
        Targets=[{"Id": "1", "Arn": func_arn, "Input": json.dumps({})}],
    )
    logger.info(f"Set EventBridge rule {rule_name} -> {schedule_expr}")


def _aws_cron(cron):
    """Convert standard 5-field cron to AWS 6-field cron expression."""
    parts = cron.strip().split()
    if len(parts) == 6:
        return cron
    if len(parts) != 5:
        logger.error(f"Invalid cron expression: '{cron}'")
        sys.exit(1)
    minute, hour, dom, month, dow = parts
    # AWS requires day-of-month and day-of-week to be mutually exclusive (one must be '?')
    if dom == "*" and dow == "*":
        dom = "*"
        dow = "?"
    elif dow != "*":
        dom = "?"
    else:
        dow = "?"
    return f"{minute} {hour} {dom} {month} {dow} *"


# OpenWhisk


def set_timer_openwhisk(workflow_data, target, cron, unset):
    _, server_name = get_faas_type(workflow_data, target)
    server = workflow_data["ComputeServers"][server_name]
    api_host = server["Endpoint"]

    workflow_name = workflow_data.get("WorkflowName", "default")
    function_name = f"{workflow_name}-{target}"
    trigger_name = f"{function_name}-timer"
    rule_name = f"{function_name}-timer-rule"

    subprocess.run(f"wsk property set --apihost {api_host}", shell=True)
    ow_api_key = os.getenv("OW_APIkey")
    if ow_api_key:
        subprocess.run(f"wsk property set --auth {ow_api_key}", shell=True)
    subprocess.run("wsk property set --insecure", shell=True)

    env = os.environ.copy()
    env["GODEBUG"] = "x509ignoreCN=0"

    if unset:
        subprocess.run(
            f"wsk rule delete {rule_name} --insecure",
            shell=True,
            env=env,
        )
        subprocess.run(
            f"wsk trigger delete {trigger_name} --insecure",
            shell=True,
            env=env,
        )
        logger.info(f"Removed OpenWhisk timer trigger {trigger_name}")
        return

    # Create alarm-feed trigger
    feed_cmd = (
        f"wsk trigger create {trigger_name} "
        f"--feed /whisk.system/alarms/alarm "
        f"-p cron '{cron}' --insecure"
    )
    result = subprocess.run(
        feed_cmd, shell=True, capture_output=True, text=True, env=env
    )
    if result.returncode != 0 and "already exists" not in result.stderr:
        logger.error(f"Failed to create trigger: {result.stderr}")
        sys.exit(1)

    rule_cmd = (
        f"wsk rule create {rule_name} {trigger_name} {function_name} --insecure"
    )
    result = subprocess.run(
        rule_cmd, shell=True, capture_output=True, text=True, env=env
    )
    if result.returncode != 0 and "already exists" not in result.stderr:
        logger.error(f"Failed to create rule: {result.stderr}")
        sys.exit(1)

    logger.info(f"Set OpenWhisk timer trigger {trigger_name} -> {cron}")


# Google Cloud 


def set_timer_gcp(workflow_data, target, cron, unset):
    gcp_secret_key = os.getenv("GCP_SecretKey")
    if not gcp_secret_key:
        logger.error("GCP_SecretKey environment variable not set")
        sys.exit(1)

    from FaaSr_py.helpers.gcp_auth import refresh_gcp_access_token

    workflow_name = workflow_data.get("WorkflowName")
    if not workflow_name:
        logger.error("WorkflowName not specified in workflow file")
        sys.exit(1)

    _, server_name = get_faas_type(workflow_data, target)
    server = workflow_data["ComputeServers"][server_name].copy()
    server["SecretKey"] = gcp_secret_key

    temp_payload = {"ComputeServers": {server_name: server}}
    access_token = refresh_gcp_access_token(temp_payload, server_name)

    project = server["Namespace"]
    region = server["Region"]
    job_name = f"{workflow_name}-{target}"
    schedule_name = f"{job_name}-timer"

    scheduler_base = (
        f"https://cloudscheduler.googleapis.com/v1/projects/{project}"
        f"/locations/{region}/jobs"
    )
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }

    full_name = f"projects/{project}/locations/{region}/jobs/{schedule_name}"

    if unset:
        resp = requests.delete(f"{scheduler_base}/{schedule_name}", headers=headers)
        if resp.status_code in [200, 204, 404]:
            logger.info(f"Removed Cloud Scheduler job {schedule_name}")
        else:
            logger.error(f"Failed to delete scheduler job: {resp.text}")
            sys.exit(1)
        return

    service_account = server.get("ClientEmail")
    if not service_account:
        logger.error("ClientEmail required for GoogleCloud server")
        sys.exit(1)

    run_job_uri = (
        f"https://run.googleapis.com/v2/projects/{project}"
        f"/locations/{region}/jobs/{job_name}:run"
    )

    body = {
        "name": full_name,
        "schedule": cron,
        "timeZone": "UTC",
        "httpTarget": {
            "uri": run_job_uri,
            "httpMethod": "POST",
            "oauthToken": {"serviceAccountEmail": service_account},
        },
    }

    resp = requests.post(scheduler_base, json=body, headers=headers)
    if resp.status_code in [200, 201]:
        logger.info(f"Created Cloud Scheduler job {schedule_name} -> {cron}")
    elif resp.status_code == 409:
        resp = requests.patch(
            f"{scheduler_base}/{schedule_name}", json=body, headers=headers
        )
        if resp.status_code in [200, 201]:
            logger.info(f"Updated Cloud Scheduler job {schedule_name} -> {cron}")
        else:
            logger.error(f"Failed to update scheduler job: {resp.text}")
            sys.exit(1)
    else:
        logger.error(f"Failed to create scheduler job: {resp.text}")
        sys.exit(1)


# Dispatcher


def main():
    args = parse_arguments()
    workflow_data = read_workflow_file(args.workflow_file)

    if not args.unset and not args.cron:
        logger.error("--cron is required unless --unset is specified")
        sys.exit(1)

    target = resolve_target(workflow_data, args.target)
    faas_type, _ = get_faas_type(workflow_data, target)

    logger.info(
        f"{'Unsetting' if args.unset else 'Setting'} timer for '{target}' "
        f"on {faas_type}"
        + ("" if args.unset else f" with cron '{args.cron}'")
    )

    if faas_type == "GitHubActions":
        set_timer_github(workflow_data, target, args.cron, args.unset, args.workflow_file)
    elif faas_type == "Lambda":
        set_timer_lambda(workflow_data, target, args.cron, args.unset)
    elif faas_type == "OpenWhisk":
        set_timer_openwhisk(workflow_data, target, args.cron, args.unset)
    elif faas_type == "GoogleCloud":
        set_timer_gcp(workflow_data, target, args.cron, args.unset)
    else:
        logger.error(f"Unsupported FaaSType for timer: {faas_type}")
        sys.exit(1)


if __name__ == "__main__":
    main()
