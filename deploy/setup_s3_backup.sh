#!/usr/bin/env bash
# One-time AWS setup for off-server backups. Safe to run again. Paste into AWS CloudShell (it's already logged in; region
# doesn't matter), or run with any admin AWS CLI login. Creates:
#   - a private S3 bucket in us-west-2: public access blocked, encrypted, HTTPS only, backups deleted after 30 days
#   - an IAM role for this server that can only read and add backups in that one bucket (it can't delete them)
#   - attaches the role to the EC2 instance, with no key stored on the server
#   - sets the instance metadata hop limit to 1, so the app's containers can't borrow the role
# It prints the bucket name at the end. Nothing here touches the running app.
set -euo pipefail

REGION=us-west-2
SERVER_IP=203.0.113.10
ROLE=dialmind-backup
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET=dialmind-backups-$ACCOUNT

echo "== Bucket $BUCKET"
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
        --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
fi
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration \
    '{"Rules":[{"ID":"delete-after-30-days","Status":"Enabled","Filter":{"Prefix":""},"Expiration":{"Days":30},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":1}}]}'
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"HttpsOnly\",\"Effect\":\"Deny\",\"Principal\":\"*\",\"Action\":\"s3:*\",\"Resource\":[\"arn:aws:s3:::$BUCKET\",\"arn:aws:s3:::$BUCKET/*\"],\"Condition\":{\"Bool\":{\"aws:SecureTransport\":\"false\"}}}]}"

echo "== Role $ROLE (this bucket only)"
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
    aws iam create-role --role-name "$ROLE" --description "DialMind server: upload database backups" \
        --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
fi
aws iam put-role-policy --role-name "$ROLE" --policy-name backup-bucket-only --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"s3:PutObject\",\"s3:GetObject\",\"s3:AbortMultipartUpload\"],\"Resource\":\"arn:aws:s3:::$BUCKET/*\"},{\"Effect\":\"Allow\",\"Action\":\"s3:ListBucket\",\"Resource\":\"arn:aws:s3:::$BUCKET\"}]}"
if ! aws iam get-instance-profile --instance-profile-name "$ROLE" >/dev/null 2>&1; then
    aws iam create-instance-profile --instance-profile-name "$ROLE" >/dev/null
    aws iam add-role-to-instance-profile --instance-profile-name "$ROLE" --role-name "$ROLE"
    echo "Waiting for IAM to catch up..."
    sleep 15
fi

echo "== Attaching it to the server ($SERVER_IP)"
INSTANCE=$(aws ec2 describe-addresses --region "$REGION" --public-ips "$SERVER_IP" --query 'Addresses[0].InstanceId' --output text)
[ -n "$INSTANCE" ] && [ "$INSTANCE" != None ] || { echo "No instance found behind $SERVER_IP" >&2; exit 1; }
CURRENT=$(aws ec2 describe-iam-instance-profile-associations --region "$REGION" --filters "Name=instance-id,Values=$INSTANCE" \
    --query 'IamInstanceProfileAssociations[0].IamInstanceProfile.Arn' --output text)
case "$CURRENT" in
*"/$ROLE") echo "Already attached." ;;
None) aws ec2 associate-iam-instance-profile --region "$REGION" --instance-id "$INSTANCE" \
    --iam-instance-profile Name="$ROLE" >/dev/null ;;
*) echo "The server already has another role ($CURRENT). Not replacing it: tell me what you'd like to do." >&2; exit 1 ;;
esac
aws ec2 modify-instance-metadata-options --region "$REGION" --instance-id "$INSTANCE" \
    --http-tokens required --http-put-response-hop-limit 1 >/dev/null

echo
echo "DONE. Bucket name: $BUCKET"
