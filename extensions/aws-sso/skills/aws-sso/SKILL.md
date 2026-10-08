---
name: aws-sso
description: Use the AWS account and role this environment is already signed in to (AWS IAM Identity Center / SSO) with the `aws` CLI v2, boto3 or any AWS SDK. Use whenever a task touches AWS -- S3, EC2, IAM, CloudWatch, Lambda, ECS, EKS, CloudFormation, Terraform/CDK against AWS, "which account am I in", AWS credentials or permission errors. Covers how to confirm the active role, how to list and use the other active roles as named profiles (`--profile`), what not to do (no `aws configure`, no `aws sso login`) and what to tell the user when credentials are missing or expired.
triggers: aws, aws cli, boto3, s3, ec2, iam, cloudformation, cloudwatch, lambda, eks, ecs, terraform aws, sso, credenciais aws, conta aws, papel aws
---

# AWS access in this environment

This environment gets temporary AWS credentials for **one or more IAM roles**
at the same time, chosen by the human user in KrewHub (the "AWS SSO" card in
the lobby). You do not log in, pick roles or manage credentials yourself.

## How credentials reach you

- One role is the **default**: `AWS_CONTAINER_CREDENTIALS_FULL_URI` and
  `AWS_CONTAINER_AUTHORIZATION_TOKEN` are already set, and every AWS SDK and
  the CLI use it automatically when you pass no profile.
- Every active role, the default included, also exists as a **named profile**
  called `<12-digit account id>:<role name>` (for example
  `123456789012:AWS-ReadOnly`). They live in a read-only file that
  `AWS_CONFIG_FILE` points to; do not edit it.
- List the active roles with `aws configure list-profiles`. Select one with
  `--profile <name>` or `AWS_PROFILE=<name>`; without either you get the
  default role. Quote the name if your shell needs it.
- `AWS_REGION` / `AWS_DEFAULT_REGION` are set to the user's default region.
  Pass `--region <r>` (CLI) or `region_name=` (boto3) for another one.
- Never run `aws configure`, `aws sso login` or `aws sso configure`, and do not
  write access keys anywhere: signing in and choosing roles happens in KrewHub.
- The `aws` CLI v2 is on `PATH` (`/opt/krewhub-ext/aws-sso/bin/aws` if it is
  not found). The directory is read-only; you cannot upgrade it.

## First step: confirm who you are

```sh
aws configure list-profiles
aws sts get-caller-identity                                  # default role
aws sts get-caller-identity --profile 123456789012:AWS-ReadOnly
```

`Account` is the AWS account id and `Arn` contains the role name
(`arn:aws:sts::<account>:assumed-role/<role>/...`). State the account and role
you are about to use before running anything that changes resources. When
several roles are active and the task does not say which account it targets,
ask the user instead of guessing.

## Using it

```sh
aws s3 ls
aws ec2 describe-instances --region us-east-2 --output table
aws s3 ls --profile 123456789012:AWS-ReadOnly
```

```python
import boto3

s3 = boto3.client("s3")  # default role: no keys, the default chain finds the credentials
other = boto3.Session(profile_name="123456789012:AWS-ReadOnly").client("s3")
```

`boto3` is not preinstalled; `pip install --user boto3` installs it in your
home directory. Terraform, CDK and other tools that use the AWS SDK default
chain work the same way (`AWS_PROFILE` selects a role for them too), with no extra configuration.

## Safety

- The role is the user's own identity. Prefer read-only calls, and ask the user
  before creating, modifying or deleting anything.
- Never print, log, commit or send `AWS_CONTAINER_AUTHORIZATION_TOKEN` or any
  credential values.

## When it does not work

Errors such as `Unable to locate credentials`, `ExpiredToken`,
`Connection refused` to `127.0.0.1`, `credentials for '<profile>' are not loaded`,
`The config profile (...) could not be found`, `AccessDenied` on an identity
call or an empty/failed `sts get-caller-identity` mean the user has to act in
KrewHub: sign in to AWS SSO, choose roles, or reload the credentials. You cannot fix
this from the shell, and retrying in a loop will not help. Tell the user what
failed and ask them to open the AWS SSO card in the KrewHub lobby, then retry
once.

`AccessDenied` on a specific API call (while `sts get-caller-identity` works)
is a permissions limit of the role you used. Report the exact action and
resource that was denied; if another active profile might allow it, say so and
ask before switching. Do not try to work around it.

To add or drop roles, or to change the default, the user edits the selection in
the same card. New CLI invocations pick it up; a long-running process (a
script, a dev server) may keep using the old credentials until it is
restarted, so re-run `aws configure list-profiles` and
`aws sts get-caller-identity` afterwards to confirm.
