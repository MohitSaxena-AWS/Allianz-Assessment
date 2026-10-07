# Allianz AWS VPC API

A Python AWS serverless solution that creates one IPv4 VPC and two or more subnets, persists the outcome, and exposes authenticated retrieval APIs.

## What the solution demonstrates

- API Gateway REST API with three routes: POST /vpcs, GET /vpcs, and GET /vpcs/{requestId}.
- Amazon Cognito User Pool authentication. Every valid authenticated user can use every route; no groups or roles are required at the API layer.
- A Python 3.12 Lambda function that validates CIDRs and availability zones, creates a VPC and multiple subnets with tags, and writes status/result records to DynamoDB.
- Idempotency through an optional clientRequestId. Repeating the same ID returns the original DynamoDB record instead of creating another network.
- Best-effort rollback. If subnet creation fails after the VPC exists, the function deletes created subnets and then tries to delete the VPC.
- Pay-per-request DynamoDB, encryption at rest, point-in-time recovery, X-Ray tracing, least-privilege application IAM permissions, and focused unit tests.

## Architecture

    Authenticated user
      -> Amazon Cognito User Pool (issues ID token)
      -> API Gateway REST API (Cognito authorizer)
      -> Lambda: validate / create / retrieve
           -> Amazon EC2 VPC APIs
           -> DynamoDB request record

The API authorizer checks only that a caller is authenticated. The Lambda stores requesterSub for audit evidence, but deliberately does not filter reads by user because the requirement says all authenticated users are authorized.

## Project layout

    template.yaml                 AWS SAM infrastructure
    src/app.py                    Lambda handler and business logic
    tests/test_app.py             Unit tests for validation and API behavior
    docs/request-examples.json    Ready-to-use request and response samples
    samconfig.toml                Default deploy configuration

## API contract

### POST /vpcs

Create a VPC with multiple subnets.

~~~~json
{
  "clientRequestId": "allianz-demo-001",
  "name": "allianz-demo",
  "vpcCidr": "10.50.0.0/16",
  "subnets": [
    {"name": "app-a", "cidr": "10.50.1.0/24", "availabilityZone": "ap-south-1a", "tier": "private"},
    {"name": "app-b", "cidr": "10.50.2.0/24", "availabilityZone": "ap-south-1b", "tier": "private"}
  ]
}
~~~~

Rules:

- name and subnet names contain letters, numbers, and hyphens only.
- VPC and subnet CIDRs are strict IPv4 network CIDRs. The implementation accepts prefix lengths from /16 through /28.
- There must be 2-12 subnets; subnet CIDRs must be inside the VPC CIDR and must not overlap.
- Every requested availability zone must be available in the deployed AWS Region.
- tier is public or private; it is stored as a subnet tag. This exercise does not create internet gateways, NAT gateways, or route tables, so public is a classification rather than public internet access.

Responses:

- 201: a newly provisioned VPC/subnet record.
- 200: an existing record when the same clientRequestId is retried.
- 400: malformed body or invalid network design.
- 502: AWS rejected the provisioning action.
- 500: unexpected application or persistence error.

### GET /vpcs?limit=25

Returns up to 1-100 recorded provisioning requests. For an interview-sized implementation this uses DynamoDB Scan; a production version should add a date-based GSI and cursor pagination.

### GET /vpcs/{requestId}

Returns one stored request record, including its IN_PROGRESS, SUCCEEDED, or FAILED status.

## Prerequisites

- AWS CLI configured with an account and Region.
- AWS SAM CLI.
- Python 3.12 for local tests.
- IAM permission to deploy CloudFormation, API Gateway, Lambda, DynamoDB, Cognito, IAM roles, and the EC2 operations named in template.yaml.

## Deploy

Use an empty or non-production AWS account because this application creates real VPC resources.

~~~~powershell
sam validate
sam build
sam deploy --guided
~~~~

On the first run, choose an AWS Region and accept the suggested S3 managed bucket. SAM saves answers into samconfig.toml. Capture the ApiUrl, UserPoolId, and UserPoolClientId stack outputs.

For later non-interactive deployment:

~~~~powershell
sam build
sam deploy
~~~~

## Create an API user and call the API

Replace the placeholder values with stack outputs. The user is required to set a permanent password once.

~~~~powershell
aws cognito-idp admin-create-user --user-pool-id <UserPoolId> --username candidate@example.com --user-attributes Name=email,Value=candidate@example.com Name=email_verified,Value=true
aws cognito-idp admin-set-user-password --user-pool-id <UserPoolId> --username candidate@example.com --password 'Replace-With-A-Strong-Password1!' --permanent
~~~~

Get an ID token and submit the example request:

~~~~powershell
$auth = aws cognito-idp initiate-auth --client-id <UserPoolClientId> --auth-flow USER_PASSWORD_AUTH --auth-parameters USERNAME=candidate@example.com,PASSWORD='Replace-With-A-Strong-Password1!' | ConvertFrom-Json
$idToken = $auth.AuthenticationResult.IdToken
$body = Get-Content .\docs\request-examples.json -Raw | ConvertFrom-Json
$create = $body.createVpcRequest | ConvertTo-Json -Depth 10
Invoke-RestMethod -Uri '<ApiUrl>/vpcs' -Method Post -Headers @{ Authorization = $idToken } -ContentType 'application/json' -Body $create
~~~~

Read the saved records:

~~~~powershell
Invoke-RestMethod -Uri '<ApiUrl>/vpcs?limit=25' -Headers @{ Authorization = $idToken }
Invoke-RestMethod -Uri '<ApiUrl>/vpcs/<requestId>' -Headers @{ Authorization = $idToken }
~~~~

## Test locally

~~~~powershell
python -m unittest discover -s tests -v
sam validate
~~~~

The tests do not create AWS resources. They mock DynamoDB and test the pure CIDR-validation behavior.

## Serverless automation bonus

The project already automates infrastructure creation with AWS SAM and CloudFormation. To automate the whole release lifecycle:

1. Store this source in GitHub or CodeCommit.
2. Use AWS CodePipeline as the trigger and orchestration service.
3. Use AWS CodeBuild to run python -m unittest discover -s tests -v, sam validate, sam build, and sam deploy --no-confirm-changeset.
4. Use a CloudFormation change set plus a manual approval action for production.
5. Add Amazon EventBridge scheduled rules for compliance checks, such as inspecting records stuck in IN_PROGRESS.
6. Send failed Lambda invocations and CloudFormation deployment failures to Amazon SNS.
7. For long-running or more complex network provisioning, put AWS Step Functions in front of the EC2 calls. Each state can create a VPC or subnet, record progress, retry transient failures, and run a compensating delete state.

For production, also restrict Lambda EC2 permissions with tag-based IAM conditions or a dedicated VPC provisioning account, add CloudTrail/CloudWatch alarms, define a DynamoDB retention policy for audit data, and use an asynchronous Step Functions workflow when network setup becomes large.

## Cleanup

Delete any VPCs created by successful API calls before deleting the stack. A VPC cannot be deleted while its subnets still exist.

~~~~powershell
aws cloudformation delete-stack --stack-name allianz-vpc-api
~~~~
