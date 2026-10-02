---
name: aws-sso
description: Use the AWS account and role this environment is already signed in to (AWS IAM Identity Center / SSO) with the `aws` CLI v2, boto3 or any AWS SDK. Use whenever a task touches AWS -- S3, EC2, IAM, CloudWatch, Lambda, ECS, EKS, CloudFormation, Terraform/CDK against AWS, "which account am I in", AWS credentials or permission errors. Covers how to confirm the active role, what not to do (no `aws configure`, no `aws sso login`, no --profile) and what to tell the user when credentials are missing or expired.
triggers: aws, aws cli, boto3, s3, ec2, iam, cloudformation, cloudwatch, lambda, eks, ecs, terraform aws, sso, credenciais aws, conta aws, papel aws
---

# AWS access in this environment

This environment gets temporary AWS credentials for **one IAM role at a time**,
chosen by the human user in KrewHub (the "AWS SSO" card in the lobby). You do
not log in, pick roles or manage credentials yourself.

## How credentials reach you

- `AWS_CONTAINER_CREDENTIALS_FULL_URI` and `AWS_CONTAINER_AUTHORIZATION_TOKEN`
  are already set. They point at a local credentials endpoint, and every AWS
  SDK and the CLI use it automatically through the default credential chain.
- `AWS_REGION` / `AWS_DEFAULT_REGION` are set to the user's default region.
  Pass `--region <r>` (CLI) or `region_name=` (boto3) for another one.
- There are **no profiles** and no `~/.aws` files. Do not pass `--profile`,
  do not run `aws configure`, `aws sso login` or `aws sso configure`, and do
  not write access keys anywhere.
- The `aws` CLI v2 is on `PATH` (`/opt/krewhub-ext/aws-sso/bin/aws` if it is
  not found). The directory is read-only; you cannot upgrade it.

## First step: confirm who you are

```sh
aws sts get-caller-identity
```

`Account` is the AWS account id and `Arn` contains the role name
(`arn:aws:sts::<account>:assumed-role/<role>/...`). That is the only role
available to you right now. State the account and role to the user before
running anything that changes resources.

## Using it

```sh
aws s3 ls
aws ec2 describe-instances --region us-east-2 --output table
```

```python
import boto3

s3 = boto3.client("s3")  # no keys, no profile: the default chain finds the credentials
```

`boto3` is not preinstalled; `pip install --user boto3` installs it in your
home directory. Terraform, CDK and other tools that use the AWS SDK default
chain work the same way, with no extra configuration.

## Safety

- The role is the user's own identity. Prefer read-only calls, and ask the user
  before creating, modifying or deleting anything.
- Never print, log, commit or send `AWS_CONTAINER_AUTHORIZATION_TOKEN` or any
  credential values.

## When it does not work

Errors such as `Unable to locate credentials`, `ExpiredToken`,
`Connection refused` to `127.0.0.1`, `AccessDenied` on an identity call or an
empty/failed `sts get-caller-identity` mean the user has to act in KrewHub:
sign in to AWS SSO, choose a role, or reload the credentials. You cannot fix
this from the shell, and retrying in a loop will not help. Tell the user what
failed and ask them to open the AWS SSO card in the KrewHub lobby, then retry
once.

`AccessDenied` on a specific API call (while `sts get-caller-identity` works)
is a permissions limit of the current role. Report the exact action and
resource that was denied, and ask the user whether they can switch to a role
that allows it. Do not try to work around it.

To use a different role or account, the user switches the role in the same
card. New CLI invocations pick it up; a long-running process (a script, a dev
server) may keep using the old credentials until it is restarted, so re-run
`aws sts get-caller-identity` afterwards to confirm.
