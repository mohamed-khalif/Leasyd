# A small Ruby web service (Sinatra) to try Leasyd's Ruby monitoring: run it with OpenTelemetry
# (run.sh) and it sends its traces and runtime metrics (CPU, memory, threads, garbage collection,
# object heap). It calls itself a few times a second, so there is always traffic.
require "net/http"
require "json"
require "opentelemetry/sdk"
require "opentelemetry/exporter/otlp"
require "opentelemetry/instrumentation/all"
require "opentelemetry-metrics-sdk"
require "opentelemetry-exporter-otlp-metrics"
require_relative "leasyd_runtime_metrics"

# With the metrics SDK loaded, configure also exports metrics (OTEL_METRICS_EXPORTER, default otlp).
OpenTelemetry::SDK.configure do |c|
  c.use_all("OpenTelemetry::Instrumentation::Sinatra" => {}, "OpenTelemetry::Instrumentation::Rack" => {})
end
LeasydRuntimeMetrics.start

require "sinatra/base"

class Orders < Sinatra::Base
  set :port, Integer(ENV.fetch("PORT", "4567"))
  set :logging, false
  CACHE = []   # keeps some objects alive, so the garbage collector has work to do

  get "/orders/:id" do
    CACHE << Array.new(rand(200..3000)) { |i| { id: params[:id], line: i, note: "x" * 20 } }
    CACHE.shift while CACHE.size > 150
    sleep(rand(0.005..0.05))
    halt 500, { error: "order store unavailable" }.to_json if rand < 0.02
    content_type :json
    { id: params[:id], lines: CACHE.last.size }.to_json
  end
end

Thread.new do
  sleep 3
  loop do
    begin
      Net::HTTP.get(URI("http://127.0.0.1:#{Orders.port}/orders/#{rand(1..500)}"))
    rescue StandardError => e
      warn "call failed: #{e}"
    end
    sleep(rand(0.15..0.5))
  end
end

Orders.run!
