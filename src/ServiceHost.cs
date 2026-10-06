using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Net;
using System.Net.Sockets;
using System.Security.AccessControl;
using System.Security.Principal;
using System.ServiceProcess;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

internal sealed class ServiceConfig
{
    internal string ServiceName;
    internal string ServiceSid;
    internal string AgentSid;
    internal string PipeName;
    internal string NodePath;
    internal string InstallRoot;
    internal string ConfigPath;
    internal string LogPath;
    internal int McpPort;
    internal string McpTokenPath;
    internal bool PythonWorker;
    internal string PythonPath;
    internal string PythonScriptPath;

    internal static ServiceConfig Load(string path)
    {
        var values = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(path, Encoding.UTF8));
        Func<string, string> field = key => values.ContainsKey(key) ? values[key] as string : null;
        var config = new ServiceConfig {
            ServiceName = field("serviceName"), ServiceSid = field("serviceAccountSid"),
            AgentSid = field("agentSid"), PipeName = field("pipeName"),
            NodePath = field("nodePath"), InstallRoot = field("installRoot"),
            ConfigPath = Path.GetFullPath(path),
            LogPath = Path.Combine(Path.GetDirectoryName(path), "logs", "service.log"),
            McpPort = values.ContainsKey("mcpPort") && values["mcpPort"] is int ? (int)values["mcpPort"] : 0,
            McpTokenPath = Path.Combine(Path.GetDirectoryName(Path.GetFullPath(path)), "mcp-token")
        };
        var workerLanguage = field("workerLanguage") ?? "node";
        if (workerLanguage != "node" && workerLanguage != "python")
            throw new InvalidDataException("Unknown worker language.");
        config.PythonWorker = workerLanguage == "python";
        config.PythonPath = Path.Combine(Path.GetDirectoryName(config.ConfigPath), "runtime", "Scripts", "python.exe");
        config.PythonScriptPath = Path.Combine(config.InstallRoot ?? "", "folder_worker.py");
        if (String.IsNullOrWhiteSpace(config.ServiceName) || String.IsNullOrWhiteSpace(config.PipeName) ||
            String.IsNullOrWhiteSpace(config.ServiceSid) || String.IsNullOrWhiteSpace(config.AgentSid) ||
            !Path.IsPathRooted(config.InstallRoot) ||
            (config.PythonWorker
                ? !File.Exists(config.PythonPath) || !File.Exists(config.PythonScriptPath)
                : !Path.IsPathRooted(config.NodePath) || !File.Exists(config.NodePath) ||
                    !File.Exists(Path.Combine(config.InstallRoot, "folder-worker.js")))) {
            throw new InvalidDataException("Invalid installed Task Folder MCP configuration.");
        }
        if (config.PythonWorker) {
            var protectedRoot = Path.GetDirectoryName(config.ConfigPath);
            var expectedBin = Path.Combine(protectedRoot, "bin");
            if (!Path.GetFullPath(config.InstallRoot).TrimEnd('\\').Equals(expectedBin.TrimEnd('\\'),
                    StringComparison.OrdinalIgnoreCase) ||
                (File.GetAttributes(expectedBin) & FileAttributes.ReparsePoint) != 0 ||
                (File.GetAttributes(Path.Combine(protectedRoot, "runtime")) & FileAttributes.ReparsePoint) != 0 ||
                (File.GetAttributes(Path.Combine(protectedRoot, "runtime", "Scripts")) & FileAttributes.ReparsePoint) != 0 ||
                (File.GetAttributes(config.PythonPath) & FileAttributes.ReparsePoint) != 0 ||
                (File.GetAttributes(config.PythonScriptPath) & FileAttributes.ReparsePoint) != 0)
                throw new InvalidDataException("Python worker must be a plain protected Tracker copy.");
        }
        new SecurityIdentifier(config.ServiceSid);
        new SecurityIdentifier(config.AgentSid);
        if (values.ContainsKey("mcpPort") && (config.McpPort < 1 || config.McpPort > 65535 ||
            !File.Exists(config.McpTokenPath)))
            throw new InvalidDataException("Invalid MCP HTTP configuration.");
        return config;
    }
}

internal static class ProtectedRootAcl
{
    internal static void Repair(string configPath)
    {
        var root = Path.GetDirectoryName(Path.GetFullPath(configPath));
        if ((File.GetAttributes(root) & FileAttributes.ReparsePoint) != 0)
            throw new InvalidDataException("Protected root is a reparse point.");
        var serviceSid = WindowsIdentity.GetCurrent().User;
        var acl = new DirectorySecurity();
        acl.SetAccessRuleProtection(true, false);
        acl.SetOwner(serviceSid);
        foreach (var sid in new[] {
            new SecurityIdentifier("S-1-5-18"),
            new SecurityIdentifier("S-1-5-32-544"),
            serviceSid
        }) {
            acl.AddAccessRule(new FileSystemAccessRule(sid, FileSystemRights.FullControl,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                PropagationFlags.None, AccessControlType.Allow));
        }
        Directory.SetAccessControl(root, acl);
    }
}

internal sealed class McpHttpServer : IDisposable
{
    private readonly int port;
    private readonly byte[] token;
    private readonly Func<string, Dictionary<string, object>, Dictionary<string, object>> call;
    private readonly JavaScriptSerializer json = new JavaScriptSerializer();
    private TcpListener listener;
    private Thread thread;
    private volatile bool stopping;
    private int activeConnections;

    internal McpHttpServer(int port, string token,
        Func<string, Dictionary<string, object>, Dictionary<string, object>> call)
    {
        if (port < 1 || port > 65535 || String.IsNullOrWhiteSpace(token) || call == null)
            throw new ArgumentException("Invalid MCP HTTP configuration.");
        this.port = port;
        this.token = Encoding.UTF8.GetBytes(token);
        this.call = call;
    }

    internal void Start()
    {
        if (listener != null) throw new InvalidOperationException("MCP HTTP listener already started.");
        stopping = false;
        listener = new TcpListener(IPAddress.Loopback, port);
        listener.Start();
        thread = new Thread(Listen) { IsBackground = true, Name = "TaskFolderMcpHttp" };
        thread.Start();
    }

    internal void Stop()
    {
        stopping = true;
        if (listener != null) listener.Stop();
        if (thread != null && Thread.CurrentThread != thread) thread.Join(5000);
        listener = null;
        thread = null;
    }

    public void Dispose() { Stop(); }

    private void Listen()
    {
        while (!stopping) {
            try {
                var client = listener.AcceptTcpClient();
                if (Interlocked.Increment(ref activeConnections) > 16) {
                    Interlocked.Decrement(ref activeConnections);
                    client.Close();
                } else if (!ThreadPool.QueueUserWorkItem(state => Handle((TcpClient)state), client)) {
                    Interlocked.Decrement(ref activeConnections);
                    client.Close();
                }
            } catch (SocketException) {
                if (!stopping) throw;
            } catch (ObjectDisposedException) {
                if (!stopping) throw;
            }
        }
    }

    private bool Authorized(string header)
    {
        const string prefix = "Bearer ";
        if (header == null || !header.StartsWith(prefix, StringComparison.Ordinal)) return false;
        var supplied = Encoding.UTF8.GetBytes(header.Substring(prefix.Length));
        int difference = supplied.Length ^ token.Length;
        for (int i = 0; i < Math.Max(supplied.Length, token.Length); i++)
            difference |= (i < supplied.Length ? supplied[i] : 0) ^ (i < token.Length ? token[i] : 0);
        return difference == 0;
    }

    private static Dictionary<string, object> Map(params object[] values)
    {
        var result = new Dictionary<string, object>();
        for (int i = 0; i < values.Length; i += 2) result[(string)values[i]] = values[i + 1];
        return result;
    }

    private static object Tool(string name, string[] required, params string[] fields)
    {
        var properties = new Dictionary<string, object>();
        foreach (var field in fields) properties[field] = Map("type", field == "summary_only" ? "boolean" : "string");
        return Map("name", name, "inputSchema", Map("type", "object", "properties", properties,
            "required", required, "additionalProperties", false));
    }

    private static object[] Tools(string scope)
    {
        if (scope == "snapshots") return new object[] {
            Tool("create_result_snapshot", new[] { "key", "project_root" }, "key", "project_root"),
            Tool("get_result_snapshot", new[] { "key", "snapshot_id" }, "key", "snapshot_id", "summary_only"),
            Tool("compare_result_snapshot", new[] { "key", "snapshot_id" }, "key", "snapshot_id")
        };
        return new object[] {
            Tool("get_tasks_folder", new string[0]), Tool("get_task_projects", new string[0]),
            Tool("create_task_folder", new[] { "key" }, "key"),
            Tool("set_task_jira_host", new[] { "key", "jira_host" }, "key", "jira_host"),
            Tool("resolve_task_folder", new[] { "key" }, "key"),
            Tool("register_task_project", new[] { "project_key", "source_type" },
                "project_key", "name", "source_type", "jira_host"),
            Tool("create_local_task_folder", new[] { "project_key", "title", "statement" },
                "project_key", "title", "statement"),
            Tool("create_task_subdirectory", new[] { "key", "relative_path" }, "key", "relative_path")
        };
    }

    private static int RemainingMilliseconds(DateTime deadline)
    {
        var remaining = (int)(deadline - DateTime.UtcNow).TotalMilliseconds;
        if (remaining <= 0) throw new System.TimeoutException("MCP HTTP request timed out.");
        return remaining;
    }

    private static void ReadHeaders(TcpClient client, Stream stream, DateTime deadline,
        out string method, out string path, out string host, out string origin,
        out string authorization, out int length)
    {
        var header = new MemoryStream();
        int state = 0;
        while (state != 4) {
            client.ReceiveTimeout = RemainingMilliseconds(deadline);
            int value = stream.ReadByte();
            if (value < 0 || header.Length >= 16384) throw new InvalidDataException("Invalid MCP HTTP headers.");
            header.WriteByte((byte)value);
            state = value == "\r\n\r\n"[state] ? state + 1 : (value == '\r' ? 1 : 0);
        }
        var lines = Encoding.ASCII.GetString(header.ToArray()).Split(new[] { "\r\n" }, StringSplitOptions.None);
        var requestLine = lines[0].Split(' ');
        if (requestLine.Length != 3 || requestLine[2] != "HTTP/1.1")
            throw new InvalidDataException("Invalid MCP HTTP request line.");
        method = requestLine[0];
        path = requestLine[1];
        host = null;
        origin = null;
        authorization = null;
        length = -1;
        bool contentLengthSeen = false;
        foreach (var line in lines) {
            int separator = line.IndexOf(':');
            if (separator < 0) continue;
            var name = line.Substring(0, separator).Trim();
            var value = line.Substring(separator + 1).Trim();
            if (name.Equals("Host", StringComparison.OrdinalIgnoreCase)) {
                if (host != null) throw new InvalidDataException("Duplicate Host header.");
                host = value;
            }
            if (name.Equals("Origin", StringComparison.OrdinalIgnoreCase)) {
                if (origin != null) throw new InvalidDataException("Duplicate Origin header.");
                origin = value;
            }
            if (name.Equals("Authorization", StringComparison.OrdinalIgnoreCase)) {
                if (authorization != null) throw new InvalidDataException("Duplicate Authorization header.");
                authorization = value;
            }
            if (name.Equals("Content-Length", StringComparison.OrdinalIgnoreCase)) {
                if (contentLengthSeen || !Int32.TryParse(value, out length))
                    throw new InvalidDataException("Invalid MCP content length.");
                contentLengthSeen = true;
            }
            if (name.Equals("Transfer-Encoding", StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("MCP HTTP transfer encoding is not supported.");
        }
    }

    private static string ReadBody(TcpClient client, Stream stream, DateTime deadline, int length)
    {
        if (length < 0 || length > 1024 * 1024) throw new InvalidDataException("Invalid MCP request size.");
        var body = new byte[length];
        for (int offset = 0; offset < length;) {
            client.ReceiveTimeout = RemainingMilliseconds(deadline);
            int count = stream.Read(body, offset, length - offset);
            if (count <= 0) throw new EndOfStreamException("Incomplete MCP request.");
            offset += count;
        }
        return new UTF8Encoding(false, true).GetString(body);
    }

    private void Respond(Stream stream, int status, object data)
    {
        var body = data == null ? new byte[0] : Encoding.UTF8.GetBytes(json.Serialize(data));
        var reason = status == 200 ? "OK" : status == 202 ? "Accepted" : status == 401 ? "Unauthorized" :
            status == 404 ? "Not Found" : status == 405 ? "Method Not Allowed" : "Bad Request";
        var headers = "HTTP/1.1 " + status + " " + reason + "\r\n" +
            "Content-Type: application/json; charset=utf-8\r\n" +
            "Content-Length: " + body.Length + "\r\nConnection: close\r\n\r\n";
        var bytes = Encoding.ASCII.GetBytes(headers);
        stream.Write(bytes, 0, bytes.Length);
        if (body.Length > 0) stream.Write(body, 0, body.Length);
        stream.Flush();
    }

    private void Handle(TcpClient client)
    {
        using (client) {
        NetworkStream stream = null;
        try {
            client.SendTimeout = 30000;
            stream = client.GetStream();
            var deadline = DateTime.UtcNow.AddSeconds(30);
            string method, path, host, origin, authorization;
            int length;
            ReadHeaders(client, stream, deadline, out method, out path, out host, out origin,
                out authorization, out length);
            if (host != "127.0.0.1:" + port ||
                (origin != null && origin != "http://127.0.0.1:" + port)) {
                Respond(stream, 400, null); return;
            }
            if (!Authorized(authorization)) { Respond(stream, 401, null); return; }
            if (method != "POST") { Respond(stream, 405, null); return; }
            var scope = path == "/mcp/folders" ? "folders" : path == "/mcp/snapshots" ? "snapshots" : null;
            if (scope == null) { Respond(stream, 404, null); return; }
            var requestBody = ReadBody(client, stream, deadline, length);
            var message = json.Deserialize<Dictionary<string, object>>(requestBody);
            if (message == null || !message.ContainsKey("method") || !(message["method"] is string)) {
                Respond(stream, 400, null); return;
            }
            if (!message.ContainsKey("id")) { Respond(stream, 202, null); return; }
            var mcpMethod = (string)message["method"];
            object result;
            if (mcpMethod == "initialize") {
                var parameters = message.ContainsKey("params") ? message["params"] as Dictionary<string, object> : null;
                result = Map("protocolVersion", parameters != null && parameters.ContainsKey("protocolVersion")
                        ? parameters["protocolVersion"] : "2025-06-18", "capabilities", Map("tools", Map()),
                    "serverInfo", Map("name", scope == "folders" ? "task-folder-mcp" : "task-tracker-mcp", "version", "0.1.0"));
            } else if (mcpMethod == "ping") result = Map();
            else if (mcpMethod == "tools/list") result = Map("tools", Tools(scope));
            else if (mcpMethod == "tools/call") {
                var parameters = message.ContainsKey("params") ? message["params"] as Dictionary<string, object> : null;
                var name = parameters != null && parameters.ContainsKey("name") ? parameters["name"] as string : null;
                var allowed = false;
                foreach (Dictionary<string, object> tool in Tools(scope))
                    if ((string)tool["name"] == name) { allowed = true; break; }
                Dictionary<string, object> data;
                if (!allowed) data = Map("ok", false, "code", "UNKNOWN_TOOL");
                else {
                    var arguments = parameters != null && parameters.ContainsKey("arguments")
                        ? parameters["arguments"] as Dictionary<string, object> : null;
                    data = call(name, arguments ?? Map());
                    // The legacy pipe adapter wraps this worker's string result itself.
                    if (name == "get_tasks_folder" && data.ContainsKey("ok") &&
                        data["ok"] is bool && (bool)data["ok"] && data.ContainsKey("data") && data["data"] is string)
                        data["data"] = Map("tasksFolder", data["data"]);
                }
                bool ok = data.ContainsKey("ok") && data["ok"] is bool && (bool)data["ok"];
                var body = ok && data.ContainsKey("data") ? data["data"] :
                    Map("code", data.ContainsKey("code") ? data["code"] : "SERVICE_ERROR",
                        "message", data.ContainsKey("message") ? data["message"] : null);
                result = Map("content", new[] { Map("type", "text", "text", json.Serialize(body)) },
                    "structuredContent", body);
                if (!ok) ((Dictionary<string, object>)result)["isError"] = true;
            } else {
                Respond(stream, 200, Map("jsonrpc", "2.0", "id", message["id"],
                    "error", Map("code", -32601, "message", "Method not found"))); return;
            }
            Respond(stream, 200, Map("jsonrpc", "2.0", "id", message["id"], "result", result));
        } catch (Exception) {
            if (stream != null) try { Respond(stream, 400, null); } catch { }
        } finally {
            Interlocked.Decrement(ref activeConnections);
        }
        }
    }
}

internal sealed class PersistentWorker : IDisposable
{
    private readonly string pythonPath;
    private readonly string scriptPath;
    private readonly string configPath;
    private readonly int timeoutMs;
    private readonly object gate = new object();
    private volatile Process process;
    private volatile bool stopping;

    internal PersistentWorker(string pythonPath, string scriptPath, string configPath, int timeoutMs)
    {
        if (!Path.IsPathRooted(pythonPath) || !Path.IsPathRooted(scriptPath) ||
            !Path.IsPathRooted(configPath) || timeoutMs <= 0)
            throw new ArgumentException("Invalid persistent worker configuration.");
        this.pythonPath = pythonPath;
        this.scriptPath = scriptPath;
        this.configPath = configPath;
        this.timeoutMs = timeoutMs;
    }

    internal void Start()
    {
        lock (gate) StartIfNeeded();
    }

    private void StartIfNeeded()
    {
        if (stopping) throw new InvalidOperationException("Worker is stopping.");
        if (process != null && !process.HasExited) return;
        Reset();
        var next = new Process();
        next.StartInfo = new ProcessStartInfo(pythonPath,
            "-I \"" + scriptPath + "\" \"" + configPath + "\"") {
            WorkingDirectory = Path.GetDirectoryName(scriptPath),
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true
        };
        next.ErrorDataReceived += (sender, args) => { /* Drain stderr without recording secrets. */ };
        try {
            next.Start();
            next.BeginErrorReadLine();
            process = next;
        } catch {
            next.Dispose();
            throw;
        }
    }

    internal string Call(string request)
    {
        lock (gate) {
            StartIfNeeded();
            try {
                var deadline = DateTime.UtcNow.AddMilliseconds(timeoutMs);
                Wait(process.StandardInput.WriteLineAsync(request), deadline);
                Wait(process.StandardInput.FlushAsync(), deadline);
                var output = process.StandardOutput.ReadLineAsync();
                Wait(output, deadline);
                var response = output.Result;
                if (String.IsNullOrWhiteSpace(response) || Encoding.UTF8.GetByteCount(response) > 1024 * 1024)
                    throw new InvalidDataException("Invalid Python worker response size.");
                var data = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(response);
                if (data == null || !data.ContainsKey("ok") || !(data["ok"] is bool))
                    throw new InvalidDataException("Invalid Python worker response shape.");
                return response;
            } catch (System.TimeoutException) {
                Reset();
                throw;
            } catch (Exception error) {
                Reset();
                throw new InvalidOperationException("Python worker failed.", error);
            }
        }
    }

    private static void Wait(Task task, DateTime deadline)
    {
        var remaining = (int)(deadline - DateTime.UtcNow).TotalMilliseconds;
        if (remaining <= 0 || !task.Wait(remaining))
            throw new System.TimeoutException("Python worker timed out.");
    }

    private void Reset()
    {
        var old = process;
        process = null;
        if (old == null) return;
        try {
            if (!old.HasExited) { old.Kill(); old.WaitForExit(5000); }
        } catch (InvalidOperationException) { }
        old.Dispose();
    }

    internal void Stop()
    {
        stopping = true;
        var current = process;
        if (current != null) try { if (!current.HasExited) current.Kill(); } catch (InvalidOperationException) { }
        lock (gate) Reset();
    }

    public void Dispose() { Stop(); }
}

internal sealed class TaskFolderService : ServiceBase
{
    private readonly ServiceConfig config;
    private readonly object sync = new object();
    private readonly object workerGate = new object();
    private Thread listener;
    private NamedPipeServerStream currentPipe;
    private Process currentWorker;
    private McpHttpServer http;
    private PersistentWorker pythonWorker;
    private volatile bool stopping;

    internal TaskFolderService(ServiceConfig config)
    {
        this.config = config;
        ServiceName = config.ServiceName;
        CanStop = true;
        AutoLog = false;
    }

    protected override void OnStart(string[] args)
    {
        try {
            if (config.PythonWorker) {
                pythonWorker = new PersistentWorker(config.PythonPath, config.PythonScriptPath,
                    config.ConfigPath, 30000);
                pythonWorker.Start();
            }
            if (config.McpPort != 0) {
                http = new McpHttpServer(config.McpPort,
                    File.ReadAllText(config.McpTokenPath, Encoding.UTF8).Trim(), CallWorker);
                http.Start();
            }
            listener = new Thread(Listen) { IsBackground = true, Name = "TaskFolderMcpPipe" };
            listener.Start();
            Log("service started");
        } catch {
            if (http != null) http.Stop();
            if (pythonWorker != null) pythonWorker.Stop();
            throw;
        }
    }

    protected override void OnStop()
    {
        stopping = true;
        if (http != null) http.Stop();
        if (pythonWorker != null) pythonWorker.Stop();
        lock (sync) {
            if (currentPipe != null) currentPipe.Dispose();
            if (currentWorker != null && !currentWorker.HasExited) currentWorker.Kill();
        }
        if (listener != null) listener.Join(5000);
        Log("service stopped");
    }

    private PipeSecurity PipeAcl()
    {
        var acl = new PipeSecurity();
        acl.SetAccessRuleProtection(true, false);
        acl.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(config.ServiceSid), PipeAccessRights.FullControl, AccessControlType.Allow));
        acl.AddAccessRule(new PipeAccessRule(new SecurityIdentifier(config.AgentSid), PipeAccessRights.ReadWrite, AccessControlType.Allow));
        acl.AddAccessRule(new PipeAccessRule(new SecurityIdentifier("S-1-5-2"), PipeAccessRights.FullControl, AccessControlType.Deny));
        return acl;
    }

    private void Listen()
    {
        while (!stopping) {
            try {
                using (var pipe = new NamedPipeServerStream(config.PipeName, PipeDirection.InOut, 1,
                    PipeTransmissionMode.Byte, PipeOptions.Asynchronous, 65536, 65536, PipeAcl())) {
                    lock (sync) currentPipe = pipe;
                    pipe.WaitForConnection();
                    if (!stopping) Handle(pipe);
                    lock (sync) if (currentPipe == pipe) currentPipe = null;
                }
            } catch (ObjectDisposedException) {
                if (!stopping) Log("pipe disposed unexpectedly");
            } catch (Exception error) {
                if (!stopping) { Log("request failure: " + error.GetType().Name + ": " + error.Message); Thread.Sleep(1000); }
            }
        }
    }

    private void Handle(NamedPipeServerStream pipe)
    {
        var reader = new StreamReader(pipe, new UTF8Encoding(false), false, 4096, true);
        var writer = new StreamWriter(pipe, new UTF8Encoding(false), 4096, true) { AutoFlush = true };
        try {
            var input = reader.ReadLineAsync();
            if (!input.Wait(30000)) throw new System.TimeoutException("Client timed out.");
            var request = input.Result;
            if (request == null || Encoding.UTF8.GetByteCount(request) > 1024 * 1024) throw new InvalidDataException("Invalid request size.");
            writer.WriteLine(RunWorker(request));
        } catch (Exception error) {
            Log("request error: " + error.GetType().Name + ": " + error.Message);
            try { writer.WriteLine("{\"ok\":false,\"code\":\"SERVICE_ERROR\"}"); } catch (IOException) { }
        } finally {
            writer.Dispose();
            reader.Dispose();
        }
    }

    private Dictionary<string, object> CallWorker(string method, Dictionary<string, object> arguments)
    {
        try {
            var request = new JavaScriptSerializer().Serialize(new Dictionary<string, object> {
                { "method", method }, { "arguments", arguments }
            });
            return new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(RunWorker(request));
        } catch (Exception error) {
            Log("MCP worker error: " + error.GetType().Name + ": " + error.Message);
            return new Dictionary<string, object> { { "ok", false }, { "code", "SERVICE_ERROR" } };
        }
    }

    private string RunWorker(string request)
    {
        lock (workerGate) {
            ProtectedRootAcl.Repair(config.ConfigPath);
            if (config.PythonWorker) return pythonWorker.Call(request);
            using (var worker = new Process()) {
                worker.StartInfo = new ProcessStartInfo(config.NodePath,
                    "\"" + Path.Combine(config.InstallRoot, "folder-worker.js") + "\" \"" + config.ConfigPath + "\"") {
                    WorkingDirectory = config.InstallRoot, UseShellExecute = false, CreateNoWindow = true,
                    RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true
                };
                try {
                    lock (sync) {
                        if (stopping) throw new InvalidOperationException("Service is stopping.");
                        worker.Start();
                        currentWorker = worker;
                    }
                    worker.StandardInput.WriteLine(request);
                    worker.StandardInput.Close();
                    var output = worker.StandardOutput.ReadLineAsync();
                    if (!output.Wait(30000)) { worker.Kill(); throw new System.TimeoutException("Worker timed out."); }
                    var response = output.Result;
                    if (!worker.WaitForExit(30000)) { worker.Kill(); throw new System.TimeoutException("Worker timed out."); }
                    var error = worker.StandardError.ReadToEnd();
                    if (worker.ExitCode != 0 || String.IsNullOrWhiteSpace(response)) {
                        Log("worker failure: " + error.Trim());
                        throw new InvalidOperationException("Worker failed.");
                    }
                    return response;
                } finally {
                    lock (sync) if (currentWorker == worker) currentWorker = null;
                }
            }
        }
    }

    private void Log(string message)
    {
        try {
            Directory.CreateDirectory(Path.GetDirectoryName(config.LogPath));
            File.AppendAllText(config.LogPath, DateTimeOffset.UtcNow.ToString("o") + " " + message + Environment.NewLine);
        } catch { /* Service must still report its actual failure to SCM. */ }
    }

    private static int Main(string[] args)
    {
        try {
            if (args.Length != 2 || args[0] != "--config") throw new ArgumentException("Usage: TaskFolderMcpService.exe --config <path>");
            ProtectedRootAcl.Repair(args[1]);
            var config = ServiceConfig.Load(args[1]);
            ServiceBase.Run(new TaskFolderService(config));
            return 0;
        } catch (Exception error) {
            Console.Error.WriteLine(error.ToString());
            return 1;
        }
    }
}
