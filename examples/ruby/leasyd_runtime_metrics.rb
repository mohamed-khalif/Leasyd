# Ruby runtime metrics for OpenTelemetry (Ruby's OpenTelemetry has none of its own yet): CPU,
# memory, threads, garbage collection and the object heap, read every export. Leasyd's
# "Leasyd - Ruby" dashboard charts them. Copy this file into your app and, after configuring
# OpenTelemetry (with the opentelemetry-metrics-sdk and opentelemetry-exporter-otlp-metrics gems, as
# in app.rb), call
#
#   LeasydRuntimeMetrics.start
module LeasydRuntimeMetrics
  module_function

  def start(meter_provider = OpenTelemetry.meter_provider)
    meter = meter_provider.meter("leasyd.ruby.runtime", version: "1.0")
    # The Ruby metrics SDK (0.19) adds each observation of an observable counter to its total, so a
    # counter's callback reports the increase since the last observation, not the running total.
    counter = lambda do |name, unit, desc, &f|
      last = nil
      meter.create_observable_counter(name, unit: unit, description: desc, callback: lambda {
        now = f.call
        increase = last.nil? || now < last ? now : now - last
        last = now
        increase
      })
    end
    gauge = ->(name, unit, desc, &f) { meter.create_observable_gauge(name, unit: unit, description: desc, callback: f) }

    counter.("process.cpu.time", "s", "CPU time used by the process") { Process.clock_gettime(Process::CLOCK_PROCESS_CPUTIME_ID) }
    gauge.("process.memory.usage", "By", "Resident memory of the process") { rss_bytes }
    gauge.("process.thread.count", "{thread}", "Live Ruby threads") { Thread.list.size }
    counter.("ruby.gc.count", "{collection}", "Garbage collections (minor and major)") { GC.count }
    counter.("ruby.gc.major.count", "{collection}", "Major (full) garbage collections") { GC.stat(:major_gc_count) }
    counter.("ruby.gc.time", "s", "Time spent in garbage collection") { gc_seconds }
    gauge.("ruby.heap.live_slots", "{slot}", "Object slots in use") { GC.stat(:heap_live_slots) }
    gauge.("ruby.heap.free_slots", "{slot}", "Object slots free") { GC.stat(:heap_free_slots) }
    counter.("ruby.objects.allocated", "{object}", "Objects allocated since start") { GC.stat(:total_allocated_objects) }
  end

  def gc_seconds
    return GC.total_time / 1e9 if GC.respond_to?(:total_time)   # Ruby 3.3+: nanoseconds
    GC.stat(:time).to_f / 1000                                   # Ruby 3.1-3.2: milliseconds
  rescue ArgumentError
    0
  end

  def rss_bytes
    File.read("/proc/self/statm").split[1].to_i * 4096          # Linux: resident pages
  rescue StandardError
    `ps -o rss= -p #{Process.pid}`.to_i * 1024                   # macOS and others
  end
end
