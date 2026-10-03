// A small Java web service to try Leasyd's JVM monitoring: run it with the OpenTelemetry Java
// agent (run.sh) and it sends its traces, logs and JVM metrics (memory, garbage collection,
// threads, classes, CPU). It calls itself a few times a second, so there is always traffic.
// Plain JDK, no dependencies.
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.Random;
import java.util.concurrent.Executors;
import java.util.logging.Logger;

public class PaymentsService {
  private static final Logger log = Logger.getLogger("payments");
  private static final Random random = new Random();
  private static final ArrayDeque<byte[]> cache = new ArrayDeque<>();   // keeps some memory live, so the heap grows and gets collected

  public static void main(String[] args) throws Exception {
    int port = Integer.parseInt(System.getenv().getOrDefault("PORT", "8080"));
    HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
    server.setExecutor(Executors.newFixedThreadPool(16));
    server.createContext("/pay", ex -> {
      int status = 200;
      String body;
      try {
        work(2 + random.nextInt(40));
        if (random.nextInt(50) == 0) throw new IllegalStateException("card declined by issuer");
        body = "{\"status\":\"paid\"}";
      } catch (Exception e) {
        log.severe("payment failed: " + e.getMessage());
        status = 502;
        body = "{\"error\":\"" + e.getMessage() + "\"}";
      }
      byte[] out = body.getBytes(StandardCharsets.UTF_8);
      ex.getResponseHeaders().add("content-type", "application/json");
      ex.sendResponseHeaders(status, out.length);
      ex.getResponseBody().write(out);
      ex.close();
    });
    server.createContext("/refund", ex -> {
      work(20 + random.nextInt(120));
      log.info("refund issued");
      ex.sendResponseHeaders(204, -1);
      ex.close();
    });
    server.start();
    log.info("payments-service listening on :" + port);

    HttpClient client = HttpClient.newHttpClient();
    String[] paths = {"/pay", "/pay", "/pay", "/pay", "/refund"};
    while (true) {
      String path = paths[random.nextInt(paths.length)];
      try {
        client.send(HttpRequest.newBuilder(URI.create("http://localhost:" + port + path)).build(),
                    HttpResponse.BodyHandlers.discarding());
      } catch (Exception e) {
        log.warning("call failed: " + e);
      }
      Thread.sleep(200 + random.nextInt(300));
    }
  }

  // Busy for about `ms` milliseconds, allocating as it goes.
  private static void work(int ms) {
    long end = System.nanoTime() + ms * 1_000_000L;
    synchronized (cache) {
      cache.add(new byte[64 * 1024 + random.nextInt(256 * 1024)]);
      while (cache.size() > 400) cache.poll();
    }
    double x = 0;
    while (System.nanoTime() < end) x += Math.sqrt(random.nextDouble());
    if (x < 0) log.info("unreachable");
  }
}
