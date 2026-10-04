# AWS: Lambda (and other CloudWatch) metrics, and a sample function

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

**`lambda-sample.yaml`** is a sample function, invoked 5 times a minute, that sends each
invocation (a span with its request ID, cold start and event) and its logs to Leasyd, so the
function's page lists its invocations with payload, logs and trace. For your own functions, use
OpenTelemetry's Lambda layer: Leasyd reads its spans (`cloud.platform = aws_lambda`,
`faas.invocation_id`); to see payloads, record the event as the `aws.lambda.event` span attribute.

    aws cloudformation deploy --stack-name leasyd-lambda-sample \
      --template-file lambda-sample.yaml --capabilities CAPABILITY_IAM \
      --parameter-overrides LeasydApiKey=obs_...

Delete the stacks to stop.
