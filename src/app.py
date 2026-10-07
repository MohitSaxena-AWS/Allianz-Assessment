"""AWS Lambda handler for the VPC provisioning interview exercise."""

import base64
import ipaddress
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError

LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TABLE_NAME = os.environ.get("TABLE_NAME", "VpcRequests")
PROJECT_NAME = os.environ.get("PROJECT_NAME", "allianz-vpc-api")
ec2 = boto3.client("ec2")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_SUBNETS = 12


class ValidationError(ValueError):
    """Raised when the client request is structurally or semantically invalid."""


def utc_now() -> str:
    """Return a UTC timestamp in the unambiguous ISO-8601 format."""
    return datetime.now(UTC).isoformat()


def api_response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Create a consistent API Gateway proxy response."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, default=str),
    }


def parse_json_body(event: dict[str, Any]) -> dict[str, Any]:
    """Parse an API Gateway body and require it to be a JSON object."""
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValidationError("Request body must contain valid JSON.") from error
    if not isinstance(value, dict):
        raise ValidationError("Request body must be a JSON object.")
    return value


def require_name(value: Any, field_name: str) -> str:
    """Validate names used for VPCs and subnets so tags remain predictable."""
    if not isinstance(value, str) or not NAME_PATTERN.fullmatch(value):
        raise ValidationError(
            f"{field_name} must be 1-63 characters: letters, numbers, and hyphens."
        )
    return value


def parse_network(value: Any, field_name: str) -> ipaddress.IPv4Network:
    """Validate an IPv4 CIDR without accepting host-bit notation."""
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be an IPv4 CIDR string.")
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as error:
        raise ValidationError(f"{field_name} must be a valid network CIDR.") from error
    if network.version != 4:
        raise ValidationError(f"{field_name} must be an IPv4 CIDR.")
    return network


def validate_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the provisioning contract before creating any AWS resources."""
    name = require_name(payload.get("name"), "name")
    vpc_network = parse_network(payload.get("vpcCidr"), "vpcCidr")
    if not 16 <= vpc_network.prefixlen <= 28:
        raise ValidationError("vpcCidr prefix length must be between /16 and /28.")

    subnets = payload.get("subnets")
    if not isinstance(subnets, list) or not 2 <= len(subnets) <= MAX_SUBNETS:
        raise ValidationError(f"subnets must contain between 2 and {MAX_SUBNETS} entries.")

    normalized_subnets: list[dict[str, str]] = []
    subnet_networks: list[ipaddress.IPv4Network] = []
    subnet_names: set[str] = set()
    for index, subnet in enumerate(subnets):
        if not isinstance(subnet, dict):
            raise ValidationError(f"subnets[{index}] must be an object.")
        subnet_name = require_name(subnet.get("name"), f"subnets[{index}].name")
        if subnet_name in subnet_names:
            raise ValidationError("Each subnet name must be unique.")
        subnet_names.add(subnet_name)

        subnet_network = parse_network(subnet.get("cidr"), f"subnets[{index}].cidr")
        if not 16 <= subnet_network.prefixlen <= 28:
            raise ValidationError(f"subnets[{index}].cidr must be between /16 and /28.")
        if not subnet_network.subnet_of(vpc_network):
            raise ValidationError(f"subnets[{index}].cidr must be inside vpcCidr.")
        if any(subnet_network.overlaps(existing) for existing in subnet_networks):
            raise ValidationError("Subnet CIDRs must not overlap.")
        subnet_networks.append(subnet_network)

        az = subnet.get("availabilityZone")
        if not isinstance(az, str) or not az:
            raise ValidationError(f"subnets[{index}].availabilityZone is required.")
        tier = subnet.get("tier", "private")
        if tier not in {"public", "private"}:
            raise ValidationError(f"subnets[{index}].tier must be public or private.")
        normalized_subnets.append(
            {
                "name": subnet_name,
                "cidr": str(subnet_network),
                "availabilityZone": az,
                "tier": tier,
            }
        )

    client_request_id = payload.get("clientRequestId")
    if client_request_id is not None and (
        not isinstance(client_request_id, str)
        or not REQUEST_ID_PATTERN.fullmatch(client_request_id)
    ):
        raise ValidationError("clientRequestId must be 8-64 letters, numbers, hyphens, or underscores.")

    return {
        "name": name,
        "vpcCidr": str(vpc_network),
        "subnets": normalized_subnets,
        "clientRequestId": client_request_id,
    }


def available_zones() -> set[str]:
    """Return the names of availability zones currently usable in this Region."""
    response = ec2.describe_availability_zones(
        Filters=[{"Name": "state", "Values": ["available"]}]
    )
    return {zone["ZoneName"] for zone in response["AvailabilityZones"]}


def tags(values: dict[str, str]) -> list[dict[str, str]]:
    """Translate a simple mapping into the EC2 tag API format."""
    return [{"Key": key, "Value": value} for key, value in values.items()]


def requester_sub(event: dict[str, Any]) -> str:
    """Read the authenticated user's Cognito subject for audit information."""
    claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
    return claims.get("sub", "unknown")


def create_initial_record(
    request_id: str, request: dict[str, Any], authenticated_sub: str
) -> dict[str, Any] | None:
    """Insert an IN_PROGRESS item; return the old item if this is a retry."""
    item = {
        "requestId": request_id,
        "status": "IN_PROGRESS",
        "createdAt": utc_now(),
        "updatedAt": utc_now(),
        "requesterSub": authenticated_sub,
        "request": request,
    }
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(requestId)")
        return None
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return table.get_item(Key={"requestId": request_id}).get("Item")


def update_record(request_id: str, status: str, **attributes: Any) -> None:
    """Replace status and result fields without overwriting the original request."""
    assignments = ["#status = :status", "updatedAt = :updatedAt"]
    names = {"#status": "status"}
    values: dict[str, Any] = {":status": status, ":updatedAt": utc_now()}
    for index, (key, value) in enumerate(attributes.items()):
        placeholder = f"#field{index}"
        value_placeholder = f":value{index}"
        assignments.append(f"{placeholder} = {value_placeholder}")
        names[placeholder] = key
        values[value_placeholder] = value
    table.update_item(
        Key={"requestId": request_id},
        UpdateExpression="SET " + ", ".join(assignments),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def cleanup(vpc_id: str | None, subnet_ids: list[str]) -> list[str]:
    """Best-effort rollback for resources created before a provisioning failure."""
    errors: list[str] = []
    for subnet_id in reversed(subnet_ids):
        try:
            ec2.delete_subnet(SubnetId=subnet_id)
        except ClientError as error:
            errors.append(f"Could not delete subnet {subnet_id}: {error.response['Error']['Code']}")
    if vpc_id:
        try:
            ec2.delete_vpc(VpcId=vpc_id)
        except ClientError as error:
            errors.append(f"Could not delete VPC {vpc_id}: {error.response['Error']['Code']}")
    return errors


def provision_network(request_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Create a VPC and all requested subnets, tagging each resource for traceability."""
    requested_zones = {subnet["availabilityZone"] for subnet in request["subnets"]}
    unknown_zones = requested_zones - available_zones()
    if unknown_zones:
        raise ValidationError(
            "Unavailable availability zones: " + ", ".join(sorted(unknown_zones))
        )

    vpc_id: str | None = None
    created_subnets: list[str] = []
    try:
        vpc_response = ec2.create_vpc(
            CidrBlock=request["vpcCidr"],
            TagSpecifications=[
                {
                    "ResourceType": "vpc",
                    "Tags": tags(
                        {
                            "Name": request["name"],
                            "Project": PROJECT_NAME,
                            "ManagedBy": "allianz-vpc-api",
                            "RequestId": request_id,
                        }
                    ),
                }
            ],
        )
        vpc_id = vpc_response["Vpc"]["VpcId"]
        ec2.get_waiter("vpc_available").wait(
            VpcIds=[vpc_id], WaiterConfig={"Delay": 2, "MaxAttempts": 15}
        )

        subnet_results: list[dict[str, str]] = []
        for subnet in request["subnets"]:
            subnet_response = ec2.create_subnet(
                VpcId=vpc_id,
                CidrBlock=subnet["cidr"],
                AvailabilityZone=subnet["availabilityZone"],
                TagSpecifications=[
                    {
                        "ResourceType": "subnet",
                        "Tags": tags(
                            {
                                "Name": subnet["name"],
                                "Tier": subnet["tier"],
                                "Project": PROJECT_NAME,
                                "ManagedBy": "allianz-vpc-api",
                                "RequestId": request_id,
                            }
                        ),
                    }
                ],
            )
            subnet_id = subnet_response["Subnet"]["SubnetId"]
            created_subnets.append(subnet_id)
            subnet_results.append(
                {
                    "subnetId": subnet_id,
                    "name": subnet["name"],
                    "cidr": subnet["cidr"],
                    "availabilityZone": subnet["availabilityZone"],
                    "tier": subnet["tier"],
                }
            )
        return {"vpcId": vpc_id, "subnets": subnet_results}
    except Exception:
        rollback_errors = cleanup(vpc_id, created_subnets)
        if rollback_errors:
            LOGGER.error("Rollback was incomplete: %s", rollback_errors)
        raise


def create_vpc(event: dict[str, Any]) -> dict[str, Any]:
    """Handle POST /vpcs with validation, idempotency, provisioning, and persistence."""
    request = validate_request(parse_json_body(event))
    request_id = request["clientRequestId"] or str(uuid.uuid4())
    request.pop("clientRequestId")
    existing_item = create_initial_record(request_id, request, requester_sub(event))
    if existing_item:
        return api_response(200, existing_item)

    try:
        resources = provision_network(request_id, request)
        update_record(request_id, "SUCCEEDED", resources=resources)
        item = table.get_item(Key={"requestId": request_id})["Item"]
        return api_response(201, item)
    except ValidationError:
        update_record(request_id, "FAILED", error="Validation failed before provisioning.")
        raise
    except ClientError as error:
        message = error.response["Error"].get("Message", "AWS provisioning failed.")
        LOGGER.exception("AWS API error for request %s", request_id)
        update_record(request_id, "FAILED", error=message)
        return api_response(502, {"requestId": request_id, "status": "FAILED", "error": message})
    except Exception:
        LOGGER.exception("Unexpected provisioning error for request %s", request_id)
        update_record(request_id, "FAILED", error="Unexpected provisioning failure.")
        return api_response(
            500,
            {"requestId": request_id, "status": "FAILED", "error": "Unexpected provisioning failure."},
        )


def list_vpcs(event: dict[str, Any]) -> dict[str, Any]:
    """Handle GET /vpcs and return the most recent records first."""
    query_parameters = event.get("queryStringParameters") or {}
    raw_limit = query_parameters.get("limit", "25")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as error:
        raise ValidationError("limit must be a number between 1 and 100.") from error
    if not 1 <= limit <= 100:
        raise ValidationError("limit must be a number between 1 and 100.")

    response = table.scan(Limit=limit)
    items = sorted(response.get("Items", []), key=lambda item: item["createdAt"], reverse=True)
    return api_response(
        200,
        {
            "items": items,
            "count": len(items),
            "hasMore": "LastEvaluatedKey" in response,
        },
    )


def get_vpc(event: dict[str, Any]) -> dict[str, Any]:
    """Handle GET /vpcs/{requestId}."""
    path_parameters = event.get("pathParameters") or {}
    request_id = path_parameters.get("requestId")
    if not request_id:
        raise ValidationError("requestId path parameter is required.")
    item = table.get_item(Key={"requestId": request_id}).get("Item")
    if not item:
        return api_response(404, {"message": f"Request {request_id} was not found."})
    return api_response(200, item)


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Route API Gateway proxy events to the correct endpoint handler."""
    try:
        method = event.get("httpMethod")
        resource = event.get("resource")
        if method == "POST" and resource == "/vpcs":
            return create_vpc(event)
        if method == "GET" and resource == "/vpcs":
            return list_vpcs(event)
        if method == "GET" and resource == "/vpcs/{requestId}":
            return get_vpc(event)
        return api_response(404, {"message": "Route not found."})
    except ValidationError as error:
        return api_response(400, {"message": str(error)})
    except ClientError:
        LOGGER.exception("DynamoDB operation failed.")
        return api_response(500, {"message": "Could not read or store the request."})
