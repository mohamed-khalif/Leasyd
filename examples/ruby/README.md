# Ruby service with OpenTelemetry

`app.rb` is a small Sinatra service that calls itself a few times a second. `run.sh` starts it
with OpenTelemetry, sending its traces and runtime metrics (CPU, memory, threads, garbage
collection, object heap and allocations) straight to Leasyd.

    LEASYD_API_KEY=obs_... bash run.sh          # needs Ruby 3.1+ and Bundler

In Leasyd: **Service Map** > `orders-ruby` > **Ruby dashboard**, or **Dashboards** > *Leasyd - Ruby*.

Ruby's OpenTelemetry has no runtime metrics of its own yet, so copy `leasyd_runtime_metrics.rb`
into your app and call `LeasydRuntimeMetrics.start` after `OpenTelemetry::SDK.configure`, with the
gems in the `Gemfile` and the `OTEL_*` settings from `run.sh`.
