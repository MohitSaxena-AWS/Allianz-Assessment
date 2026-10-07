import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# AWS Lambda supplies boto3. This small stub lets the pure unit tests run offline.
if "boto3" not in sys.modules:
    class FakeClientError(Exception):
        def __init__(self, error_response, operation_name):
            self.response = error_response
            self.operation_name = operation_name
            super().__init__(error_response["Error"]["Message"])

    class FakeDynamoResource:
        @staticmethod
        def Table(_name):
            return object()

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda _service: object()
    fake_boto3.resource = lambda _service: FakeDynamoResource()
    fake_botocore = types.ModuleType("botocore")
    fake_exceptions = types.ModuleType("botocore.exceptions")
    fake_exceptions.ClientError = FakeClientError
    fake_botocore.exceptions = fake_exceptions
    sys.modules["boto3"] = fake_boto3
    sys.modules["botocore"] = fake_botocore
    sys.modules["botocore.exceptions"] = fake_exceptions

sys.path.append(str(Path(__file__).parents[1] / "src"))
import app  # noqa: E402


def valid_payload():
    return {
        "name": "allianz-demo",
        "vpcCidr": "10.50.0.0/16",
        "subnets": [
            {
                "name": "app-a",
                "cidr": "10.50.1.0/24",
                "availabilityZone": "ap-south-1a",
                "tier": "private",
            },
            {
                "name": "app-b",
                "cidr": "10.50.2.0/24",
                "availabilityZone": "ap-south-1b",
                "tier": "private",
            },
        ],
    }


def api_event(method, resource, body=None):
    return {
        "httpMethod": method,
        "resource": resource,
        "body": json.dumps(body) if body else None,
        "isBase64Encoded": False,
        "requestContext": {"authorizer": {"claims": {"sub": "user-123"}}},
    }


class VpcApiTests(unittest.TestCase):
    def test_validate_request_accepts_two_non_overlapping_subnets(self):
        result = app.validate_request(valid_payload())

        self.assertEqual(result["vpcCidr"], "10.50.0.0/16")
        self.assertEqual(len(result["subnets"]), 2)
        self.assertEqual(result["subnets"][0]["tier"], "private")

    def test_validate_request_rejects_invalid_vpc_cidr(self):
        payload = valid_payload()
        payload["vpcCidr"] = "10.50.1.1/16"

        with self.assertRaisesRegex(app.ValidationError, "valid network CIDR"):
            app.validate_request(payload)

    def test_validate_request_rejects_invalid_name(self):
        payload = valid_payload()
        payload["name"] = "name with spaces"

        with self.assertRaisesRegex(app.ValidationError, "letters, numbers, and hyphens"):
            app.validate_request(payload)

    def test_validate_request_rejects_overlapping_subnets(self):
        payload = valid_payload()
        payload["subnets"][1]["cidr"] = "10.50.1.0/24"

        with self.assertRaisesRegex(app.ValidationError, "must not overlap"):
            app.validate_request(payload)

    def test_handler_returns_400_for_invalid_json(self):
        event = api_event("POST", "/vpcs")
        event["body"] = "{not-json"

        response = app.lambda_handler(event, None)

        self.assertEqual(response["statusCode"], 400)
        self.assertIn("valid JSON", json.loads(response["body"])["message"])

    @patch.object(app, "table")
    def test_get_vpc_returns_404_when_record_does_not_exist(self, mock_table):
        mock_table.get_item.return_value = {}

        response = app.lambda_handler(
            {
                "httpMethod": "GET",
                "resource": "/vpcs/{requestId}",
                "pathParameters": {"requestId": "missing-request"},
            },
            None,
        )

        self.assertEqual(response["statusCode"], 404)
        self.assertEqual(
            mock_table.get_item.call_args.kwargs["Key"]["requestId"], "missing-request"
        )

    @patch.object(app, "table")
    def test_create_reuses_existing_idempotency_record(self, mock_table):
        existing = {"requestId": "repeatable1", "status": "SUCCEEDED"}
        mock_table.put_item.side_effect = app.ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
            "PutItem",
        )
        mock_table.get_item.return_value = {"Item": existing}
        payload = valid_payload()
        payload["clientRequestId"] = "repeatable1"

        response = app.lambda_handler(api_event("POST", "/vpcs", payload), None)

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(json.loads(response["body"]), existing)
