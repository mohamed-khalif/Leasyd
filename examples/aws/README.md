# AWS: CloudWatch metrics and logs, and a sample function

**`cloudwatch-metrics.yaml`** sends your account's CloudWatch metrics (AWS Lambda; optionally two
more namespaces such as AWS/SQS) to Leasyd every minute: a CloudWatch metric stream, through
Amazon Data Firehose, to `https://ingest.leasyd.com/v1/aws/cloudwatch-metrics`. Deploy it once per region:

    aws cloudformation deploy --stack-name leasyd-cloudwatch-metrics \
      --template-file cloudwatch-metrics.yaml --capabilities CAPABILITY_IAM \
      --parameter-overrides LeasydApiKey=obs_...

In Leasyd: **AWS Lambda** lists your functions (invocations, errors, duration), and
**Dashboards** > *Leasyd - AWS Lambda* charts them. Each function is a service; metrics are named
`aws.lambda.invocations`, `aws.lambda.duration`, ... (histograms: use `_sum` and `_count`).
AWS bills the stream ($0.003 per 1,000 metric updates; about 8 a minute per function) and
Firehose. Data Leasyd refuses is kept for 7 days in the stack's bucket.

**`cloudwatch-logs.yaml`** sends your CloudWatch Logs to Leasyd, about a minute after they're
written: subscription filters on the log groups you list, through Amazon Data Firehose, to
`https://ingest.leasyd.com/v1/aws/cloudwatch-logs`. Deploy it once per region:

    aws cloudformation deploy --stack-name leasyd-cloudwatch-logs \
      --template-file cloudwatch-logs.yaml --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
      --parameter-overrides LeasydApiKey=obs_... LogGroups=/aws/lambda/checkout,/ecs/shop

- **Which logs:** the groups in `LogGroups`. Or `AllLogGroups=true` for every group in the region,
  new ones included (an account-wide subscription policy; a region can have only one). Only new
  events are sent; older ones stay in CloudWatch.
- **Fewer logs:** `FilterPattern` uses CloudWatch's syntax, e.g. `?ERROR ?WARN`; empty sends all.
  Or add drop rules in Leasyd, e.g. Lambda's START and END lines
  (`attributes.aws.lambda.platform in start, end`).
- **Service names** come from the log group: `/aws/lambda/<function>` is the function;
  an ECS (`awslogs`) stream `<prefix>/<container>/<task>` is the container; `/aws/rds/instance/<db>/...`
  is the database; `API-Gateway-Execution-Logs_<id>/<stage>` is `apigateway-<id>`; any other group
  is its name.
- **In each log:** JSON logs give the message, level and fields; text logs a level when they start
  with one (`ERROR`, `[WARN]`, Lambda's own format). A trace id (`trace_id`, `traceId` or an X-Ray
  id) links the log to its trace. Lambda's `REPORT` lines carry `aws.lambda.duration_ms`,
  `aws.lambda.max_memory_used_mb` and `faas.coldstart`.
- **Limits:** a log group takes at most 2 subscription filters; a group that already has 2 fails
  the stack with AWS's message. A Lambda function's log group exists only after its first run.
- **Cost:** CloudWatch Logs bills ingestion as before (no extra charge for the subscription);
  Firehose about $0.029 per GB; Leasyd its usual logs price.

**`lambda-sample.yaml`** is a sample function, invoked 5 times a minute, that sends each
invocation (a span with its request ID, cold start and event) and its logs to Leasyd, so the
function's page lists its invocations with payload, logs and trace. For your own functions, use
OpenTelemetry's Lambda layer: Leasyd reads its spans (`cloud.platform = aws_lambda`,
`faas.invocation_id`); to see payloads, record the event as the `aws.lambda.event` span attribute.

    aws cloudformation deploy --stack-name leasyd-lambda-sample \
      --template-file lambda-sample.yaml --capabilities CAPABILITY_IAM \
      --parameter-overrides LeasydApiKey=obs_...

Delete the stacks to stop.
