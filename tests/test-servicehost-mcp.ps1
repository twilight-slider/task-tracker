$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not (Select-String -LiteralPath (Join-Path $repo '.gitignore') -Pattern '^/\.runtime/$' -Quiet)) {
    throw 'Root .gitignore must contain /.runtime/ before tests run.'
}
$testRoot = Join-Path $repo ('.runtime\tests\test-servicehost-mcp\run-' + $PID)
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
$source = Join-Path $testRoot 'McpHttpTest.cs'
$exe = Join-Path $testRoot 'McpHttpTest.exe'
@'
using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

internal static class McpHttpTest
{
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

    private static int FreePort()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        int port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    private static string Post(int port, string scope, string token, string body, int status,
        string host = null, string origin = null)
    {
        var request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + "/mcp/" + scope);
        request.Method = "POST";
        request.ContentType = "application/json";
        if (host != null) request.Host = host;
        if (origin != null) request.Headers["Origin"] = origin;
        if (token != null) request.Headers[HttpRequestHeader.Authorization] = "Bearer " + token;
        var bytes = Encoding.UTF8.GetBytes(body);
        request.ContentLength = bytes.Length;
        using (var stream = request.GetRequestStream()) stream.Write(bytes, 0, bytes.Length);
        HttpWebResponse response;
        try { response = (HttpWebResponse)request.GetResponse(); }
        catch (WebException error) { response = (HttpWebResponse)error.Response; }
        using (response) {
            if ((int)response.StatusCode != status) throw new Exception("Expected " + status + ", got " + (int)response.StatusCode);
            using (var reader = new StreamReader(response.GetResponseStream())) return reader.ReadToEnd();
        }
    }

    private static void GetWithoutBody(int port)
    {
        var request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + "/mcp/folders");
        request.Method = "GET";
        request.Headers[HttpRequestHeader.Authorization] = "Bearer test-token";
        try { using (var response = (HttpWebResponse)request.GetResponse()) throw new Exception("GET unexpectedly succeeded"); }
        catch (WebException error) {
            using (var response = (HttpWebResponse)error.Response)
                if ((int)response.StatusCode != 405) throw new Exception("GET must return 405");
        }
    }

    private static void UnauthorizedHeadersDoNotWaitForBody(int port)
    {
        using (var client = new TcpClient("127.0.0.1", port)) {
            client.ReceiveTimeout = 2000;
            var stream = client.GetStream();
            var headers = Encoding.ASCII.GetBytes("POST /mcp/folders HTTP/1.1\r\nHost: 127.0.0.1:" + port +
                "\r\nContent-Length: 1000000\r\n\r\n");
            stream.Write(headers, 0, headers.Length);
            var buffer = new byte[128];
            int read = stream.Read(buffer, 0, buffer.Length);
            Has(Encoding.ASCII.GetString(buffer, 0, read), "401 Unauthorized");
        }
    }

    private static void Has(string text, string expected)
    {
        if (!text.Contains(expected)) throw new Exception("Missing " + expected + " in " + text);
    }

    private static int Main(string[] args)
    {
        try { return Run(args); }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }

    private static int Run(string[] args)
    {
        int port = args.Length == 2 && args[0] == "--serve" ? Int32.Parse(args[1]) : FreePort();
        int calls = 0;
        using (var server = new McpHttpServer(port, "test-token", (method, toolArgs) => {
            calls++;
            return new Dictionary<string, object> { { "ok", true }, { "data", new Dictionary<string, object> { { "method", method } } } };
        })) {
            server.Start();
            if (args.Length == 2 && args[0] == "--serve") {
                Console.WriteLine("READY");
                Thread.Sleep(Timeout.Infinite);
            }
            const string init = "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-06-18\"}}";
            Post(port, "folders", null, init, 401);
            Post(port, "folders", "wrong", init, 401);
            Post(port, "folders", "test-token", init, 400, "evil.example");
            Post(port, "folders", "test-token", init, 400, null, "http://evil.example");
            GetWithoutBody(port);
            UnauthorizedHeadersDoNotWaitForBody(port);
            Has(Post(port, "folders", "test-token", init, 200), "serverInfo");
            Has(Post(port, "folders", "test-token", "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\"}", 200), "get_tasks_folder");
            Has(Post(port, "snapshots", "test-token", "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/list\"}", 200), "create_result_snapshot");
            Has(Post(port, "folders", "test-token", "{\"jsonrpc\":\"2.0\",\"id\":4,\"method\":\"tools/call\",\"params\":{\"name\":\"get_tasks_folder\",\"arguments\":{}}}", 200), "get_tasks_folder");
            Has(Post(port, "snapshots", "test-token", "{\"jsonrpc\":\"2.0\",\"id\":5,\"method\":\"tools/call\",\"params\":{\"name\":\"compare_result_snapshot\",\"arguments\":{}}}", 200), "compare_result_snapshot");
            Has(Post(port, "folders", "test-token", "{\"jsonrpc\":\"2.0\",\"id\":6,\"method\":\"tools/call\",\"params\":{\"name\":\"compare_result_snapshot\",\"arguments\":{}}}", 200), "UNKNOWN_TOOL");
            Post(port, "folders", "test-token", "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}", 202);
            if (calls != 2) throw new Exception("Unexpected worker calls: " + calls);
        }
        Console.WriteLine("HTTP MCP auth, scopes and calls passed");
        return 0;
    }
}
'@ | Set-Content -LiteralPath $source -Encoding UTF8
$csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $csc /nologo /target:exe /main:McpHttpTest "/out:$exe" /reference:System.ServiceProcess.dll /reference:System.Web.Extensions.dll (Join-Path $repo 'src\ServiceHost.cs') $source
if ($LASTEXITCODE -ne 0) { throw 'HTTP MCP test did not compile.' }
& $exe
if ($LASTEXITCODE -ne 0) { throw 'HTTP MCP test failed.' }
