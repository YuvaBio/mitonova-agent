# Unicore Deployment Commands

## Deploy (First Time)
```bash
./deploy-ecs.sh
```

Wait 10 minutes. Get URL:
```bash
aws elbv2 describe-load-balancers --names unicore-alb --region us-east-1 --query 'LoadBalancers[0].DNSName' --output text
```

---

## Stop Service (Save ~$25/month)
```bash
aws ecs update-service --cluster unicore-cluster --service unicore-service --desired-count 0 --region us-east-1
```

## Start Service
```bash
aws ecs update-service --cluster unicore-cluster --service unicore-service --desired-count 1 --region us-east-1
```

Wait 3 minutes after starting.

---

## Update Code
```bash
# Build and push
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $(aws sts get-caller-identity --query Account --output text).dkr.ecr.us-east-1.amazonaws.com
docker buildx build --platform linux/amd64 -f Dockerfile.aws -t $(aws sts get-caller-identity --query Account --output text).dkr.ecr.us-east-1.amazonaws.com/unicore:latest --push .

# Deploy update
aws ecs update-service --cluster unicore-cluster --service unicore-service --force-new-deployment --region us-east-1
```

---

## Delete Everything
```bash
# Delete service
aws ecs delete-service --cluster unicore-cluster --service unicore-service --force --region us-east-1
sleep 120

# Delete cluster
aws ecs delete-cluster --cluster unicore-cluster --region us-east-1

# Delete load balancer
aws elbv2 delete-load-balancer --load-balancer-arn $(aws elbv2 describe-load-balancers --names unicore-alb --region us-east-1 --query 'LoadBalancers[0].LoadBalancerArn' --output text) --region us-east-1
sleep 120

# Delete target group
aws elbv2 delete-target-group --target-group-arn $(aws elbv2 describe-target-groups --names unicore-tg --region us-east-1 --query 'TargetGroups[0].TargetGroupArn' --output text) --region us-east-1

# Delete security groups
aws ec2 delete-security-group --group-name unicore-task-sg --region us-east-1
aws ec2 delete-security-group --group-name unicore-alb-sg --region us-east-1
```

---

## Useful Commands
```bash
# Check status (1 = running, 0 = stopped)
aws ecs describe-services --cluster unicore-cluster --service unicore-service --region us-east-1 --query 'services[0].runningCount' --output text

# View logs
aws logs tail /ecs/unicore --follow --region us-east-1

# Get URL
aws elbv2 describe-load-balancers --names unicore-alb --region us-east-1 --query 'LoadBalancers[0].DNSName' --output text
```

---

**Costs:** Running: $45/mo | Stopped: $20/mo | Deleted: $0
