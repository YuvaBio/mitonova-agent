#!/bin/bash
# deploy-ecs.sh - Deploy Unicore Agent to AWS ECS Fargate

set -e

# Configuration
CLUSTER_NAME="unicore-cluster"
SERVICE_NAME="unicore-service"
TASK_FAMILY="unicore-task"
ECR_REPO="unicore"
AWS_REGION="us-east-1"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}ℹ${NC} $1"; }
log_success() { echo -e "${GREEN}✓${NC} $1"; }
log_error() { echo -e "${RED}✗${NC} $1"; }
log_warning() { echo -e "${YELLOW}⚠${NC} $1"; }

echo "╔════════════════════════════════════════════════════╗"
echo "║     Unicore Agent - ECS Fargate Deployment        ║"
echo "╚════════════════════════════════════════════════════╝"
echo ""

# Step 1: Create ECR repository
log_info "Setting up ECR repository..."
if ! aws ecr describe-repositories --repository-names ${ECR_REPO} --region ${AWS_REGION} &>/dev/null; then
    aws ecr create-repository \
        --repository-name ${ECR_REPO} \
        --region ${AWS_REGION} \
        --image-scanning-configuration scanOnPush=true > /dev/null
    log_success "ECR repository created"
else
    log_info "ECR repository already exists"
fi

# Step 2: Build and push Docker image
log_info "Building Docker image for AMD64..."
aws ecr get-login-password --region ${AWS_REGION} | \
    docker login --username AWS --password-stdin ${ECR_URI}

docker buildx build --platform linux/amd64 \
    -f Dockerfile.aws \
    -t ${ECR_URI}:latest \
    --push .

log_success "Docker image pushed to ECR"

# Step 3: Create ECS cluster
log_info "Setting up ECS cluster..."
if ! aws ecs describe-clusters --clusters ${CLUSTER_NAME} --region ${AWS_REGION} --query 'clusters[0].clusterName' --output text 2>/dev/null | grep -q ${CLUSTER_NAME}; then
    aws ecs create-cluster --cluster-name ${CLUSTER_NAME} --region ${AWS_REGION} > /dev/null
    log_success "ECS cluster created"
else
    log_info "ECS cluster already exists"
fi

# Step 4: Create IAM roles
log_info "Setting up IAM roles..."
EXECUTION_ROLE_NAME="ecsTaskExecutionRole-unicore"
TASK_ROLE_NAME="ecsTaskRole-unicore"

cat > /tmp/ecs-trust-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "ecs-tasks.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

# Create execution role
if ! aws iam get-role --role-name ${EXECUTION_ROLE_NAME} &>/dev/null; then
    aws iam create-role \
        --role-name ${EXECUTION_ROLE_NAME} \
        --assume-role-policy-document file:///tmp/ecs-trust-policy.json > /dev/null
    
    aws iam attach-role-policy \
        --role-name ${EXECUTION_ROLE_NAME} \
        --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
    
    aws iam attach-role-policy \
        --role-name ${EXECUTION_ROLE_NAME} \
        --policy-arn arn:aws:iam::aws:policy/CloudWatchLogsFullAccess
    
    log_success "Execution role created"
    sleep 10
else
    log_info "Execution role already exists"
fi

# Create task role
if ! aws iam get-role --role-name ${TASK_ROLE_NAME} &>/dev/null; then
    aws iam create-role \
        --role-name ${TASK_ROLE_NAME} \
        --assume-role-policy-document file:///tmp/ecs-trust-policy.json > /dev/null
    
    aws iam attach-role-policy \
        --role-name ${TASK_ROLE_NAME} \
        --policy-arn arn:aws:iam::aws:policy/AmazonBedrockFullAccess
    
    log_success "Task role created"
    sleep 10
else
    log_info "Task role already exists"
fi

EXECUTION_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${EXECUTION_ROLE_NAME}"
TASK_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${TASK_ROLE_NAME}"

# Step 5: Create CloudWatch log group
log_info "Setting up CloudWatch logs..."
aws logs create-log-group --log-group-name /ecs/unicore --region ${AWS_REGION} 2>/dev/null || log_info "Log group already exists"

# Step 6: Register task definition
log_info "Registering ECS task definition..."

cat > /tmp/task-definition.json <<EOF
{
  "family": "${TASK_FAMILY}",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "1024",
  "memory": "2048",
  "executionRoleArn": "${EXECUTION_ROLE_ARN}",
  "taskRoleArn": "${TASK_ROLE_ARN}",
  "containerDefinitions": [
    {
      "name": "unicore-container",
      "image": "${ECR_URI}:latest",
      "essential": true,
      "portMappings": [
        {
          "containerPort": 8000,
          "protocol": "tcp"
        }
      ],
      "environment": [
        {"name": "LOG_LEVEL", "value": "INFO"},
        {"name": "AWS_DEFAULT_REGION", "value": "us-east-1"}
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/unicore",
          "awslogs-region": "${AWS_REGION}",
          "awslogs-stream-prefix": "ecs"
        }
      },
      "healthCheck": {
        "command": ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"],
        "interval": 30,
        "timeout": 5,
        "retries": 3,
        "startPeriod": 30
      }
    }
  ]
}
EOF

aws ecs register-task-definition \
    --cli-input-json file:///tmp/task-definition.json \
    --region ${AWS_REGION} > /dev/null

log_success "Task definition registered"

# Step 7: Setup networking
log_info "Setting up networking..."

VPC_ID=$(aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" --query 'Vpcs[0].VpcId' --output text --region ${AWS_REGION})
SUBNETS=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=${VPC_ID}" --query 'Subnets[*].SubnetId' --output json --region ${AWS_REGION})
SUBNET_1=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=${VPC_ID}" --query 'Subnets[0].SubnetId' --output text --region ${AWS_REGION})
SUBNET_2=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=${VPC_ID}" --query 'Subnets[1].SubnetId' --output text --region ${AWS_REGION})
SUBNET_LIST="${SUBNET_1} ${SUBNET_2}"

# Create security groups
ALB_SG_NAME="unicore-alb-sg"
TASK_SG_NAME="unicore-task-sg"

if ! aws ec2 describe-security-groups --filters "Name=group-name,Values=${ALB_SG_NAME}" --query 'SecurityGroups[0].GroupId' --output text --region ${AWS_REGION} 2>/dev/null | grep -q sg-; then
    ALB_SG_ID=$(aws ec2 create-security-group \
        --group-name ${ALB_SG_NAME} \
        --description "Security group for Unicore ALB" \
        --vpc-id ${VPC_ID} \
        --region ${AWS_REGION} \
        --query 'GroupId' \
        --output text)
    
    aws ec2 authorize-security-group-ingress \
        --group-id ${ALB_SG_ID} \
        --protocol tcp \
        --port 80 \
        --cidr 0.0.0.0/0 \
        --region ${AWS_REGION}
    
    log_success "ALB security group created"
else
    ALB_SG_ID=$(aws ec2 describe-security-groups --filters "Name=group-name,Values=${ALB_SG_NAME}" --query 'SecurityGroups[0].GroupId' --output text --region ${AWS_REGION})
    log_info "ALB security group already exists"
fi

if ! aws ec2 describe-security-groups --filters "Name=group-name,Values=${TASK_SG_NAME}" --query 'SecurityGroups[0].GroupId' --output text --region ${AWS_REGION} 2>/dev/null | grep -q sg-; then
    TASK_SG_ID=$(aws ec2 create-security-group \
        --group-name ${TASK_SG_NAME} \
        --description "Security group for Unicore ECS tasks" \
        --vpc-id ${VPC_ID} \
        --region ${AWS_REGION} \
        --query 'GroupId' \
        --output text)
    
    aws ec2 authorize-security-group-ingress \
        --group-id ${TASK_SG_ID} \
        --protocol tcp \
        --port 8000 \
        --source-group ${ALB_SG_ID} \
        --region ${AWS_REGION}
    
    log_success "Task security group created"
else
    TASK_SG_ID=$(aws ec2 describe-security-groups --filters "Name=group-name,Values=${TASK_SG_NAME}" --query 'SecurityGroups[0].GroupId' --output text --region ${AWS_REGION})
    log_info "Task security group already exists"
fi

# Create ALB
ALB_NAME="unicore-alb"
if ! aws elbv2 describe-load-balancers --names ${ALB_NAME} --region ${AWS_REGION} &>/dev/null; then
    ALB_ARN=$(aws elbv2 create-load-balancer \
        --name ${ALB_NAME} \
        --subnets ${SUBNET_LIST} \
        --security-groups ${ALB_SG_ID} \
        --scheme internet-facing \
        --type application \
        --ip-address-type ipv4 \
        --region ${AWS_REGION} \
        --query 'LoadBalancers[0].LoadBalancerArn' \
        --output text)
    
    log_success "Load balancer created"
    sleep 30
else
    ALB_ARN=$(aws elbv2 describe-load-balancers --names ${ALB_NAME} --region ${AWS_REGION} --query 'LoadBalancers[0].LoadBalancerArn' --output text)
    log_info "Load balancer already exists"
fi

# Create target group
TG_NAME="unicore-tg"
if ! aws elbv2 describe-target-groups --names ${TG_NAME} --region ${AWS_REGION} &>/dev/null; then
    TG_ARN=$(aws elbv2 create-target-group \
        --name ${TG_NAME} \
        --protocol HTTP \
        --port 8000 \
        --vpc-id ${VPC_ID} \
        --target-type ip \
        --health-check-path /health \
        --health-check-interval-seconds 30 \
        --health-check-timeout-seconds 10 \
        --healthy-threshold-count 2 \
        --unhealthy-threshold-count 3 \
        --region ${AWS_REGION} \
        --query 'TargetGroups[0].TargetGroupArn' \
        --output text)
    
    log_success "Target group created"
else
    TG_ARN=$(aws elbv2 describe-target-groups --names ${TG_NAME} --region ${AWS_REGION} --query 'TargetGroups[0].TargetGroupArn' --output text)
    log_info "Target group already exists"
fi

# Create listener
if ! aws elbv2 describe-listeners --load-balancer-arn ${ALB_ARN} --region ${AWS_REGION} --query 'Listeners[0].ListenerArn' --output text 2>/dev/null | grep -q arn; then
    aws elbv2 create-listener \
        --load-balancer-arn ${ALB_ARN} \
        --protocol HTTP \
        --port 80 \
        --default-actions Type=forward,TargetGroupArn=${TG_ARN} \
        --region ${AWS_REGION} > /dev/null
    
    log_success "Listener created"
else
    log_info "Listener already exists"
fi

# Step 8: Create ECS service
log_info "Creating ECS service..."

cat > /tmp/service-config.json <<EOF
{
  "cluster": "${CLUSTER_NAME}",
  "serviceName": "${SERVICE_NAME}",
  "taskDefinition": "${TASK_FAMILY}",
  "desiredCount": 1,
  "launchType": "FARGATE",
  "networkConfiguration": {
    "awsvpcConfiguration": {
      "subnets": ${SUBNETS},
      "securityGroups": ["${TASK_SG_ID}"],
      "assignPublicIp": "ENABLED"
    }
  },
  "loadBalancers": [
    {
      "targetGroupArn": "${TG_ARN}",
      "containerName": "unicore-container",
      "containerPort": 8000
    }
  ],
  "healthCheckGracePeriodSeconds": 60
}
EOF

if ! aws ecs describe-services --cluster ${CLUSTER_NAME} --services ${SERVICE_NAME} --region ${AWS_REGION} --query 'services[0].serviceName' --output text 2>/dev/null | grep -q ${SERVICE_NAME}; then
    aws ecs create-service --cli-input-json file:///tmp/service-config.json --region ${AWS_REGION} > /dev/null
    log_success "ECS service created"
else
    aws ecs update-service \
        --cluster ${CLUSTER_NAME} \
        --service ${SERVICE_NAME} \
        --task-definition ${TASK_FAMILY} \
        --force-new-deployment \
        --region ${AWS_REGION} > /dev/null
    log_success "ECS service updated"
fi

# Get ALB DNS name
ALB_DNS=$(aws elbv2 describe-load-balancers --names ${ALB_NAME} --region ${AWS_REGION} --query 'LoadBalancers[0].DNSName' --output text)

echo ""
echo "╔════════════════════════════════════════════════════╗"
echo "║          Deployment Complete!                      ║"
echo "╚════════════════════════════════════════════════════╝"
echo ""
echo "Application URL: http://${ALB_DNS}"
echo "Health Check: http://${ALB_DNS}/health"
echo ""
echo "It may take 2-3 minutes for the service to become healthy."
echo ""
echo "Monitor deployment:"
echo "  aws ecs describe-services --cluster ${CLUSTER_NAME} --services ${SERVICE_NAME} --region ${AWS_REGION}"
echo ""
echo "View logs:"
echo "  aws logs tail /ecs/unicore --follow --region ${AWS_REGION}"
echo ""

# Cleanup
rm -f /tmp/ecs-trust-policy.json /tmp/task-definition.json /tmp/service-config.json