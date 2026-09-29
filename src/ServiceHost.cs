using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Security.AccessControl;
using System.Security.Principal;
using System.ServiceProcess;
using System.Text;
using System.Threading;
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

    internal static ServiceConfig Load(string path)
    {
        var values = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(path, Encoding.UTF8));
        Func<string, string> field = key => values.ContainsKey(key) ? values[key] as string : null;
        var config = new ServiceConfig {
            ServiceName = field("serviceName"), ServiceSid = field("serviceAccountSid"),
            AgentSid = field("agentSid"), PipeName = field("pipeName"),
            NodePath = field("nodePath"), InstallRoot = field("installRoot"),
            ConfigPath = Path.GetFullPath(path),
            LogPath = Path.Combine(Path.GetDirectoryName(path), "logs", "service.log")
        };
        if (String.IsNullOrWhiteSpace(config.ServiceName) || String.IsNullOrWhiteSpace(config.PipeName) ||
            String.IsNullOrWhiteSpace(config.ServiceSid) || String.IsNullOrWhiteSpace(config.AgentSid) ||
            !Path.IsPathRooted(config.NodePath) || !Path.IsPathRooted(config.InstallRoot) ||
            !File.Exists(config.NodePath) || !File.Exists(Path.Combine(config.InstallRoot, "folder-worker.js"))) {
            throw new InvalidDataException("Invalid installed Task Folder MCP configuration.");
        }
        new SecurityIdentifier(config.ServiceSid);
        new SecurityIdentifier(config.AgentSid);
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

internal sealed class TaskFolderService : ServiceBase
{
    private readonly ServiceConfig config;
    private readonly object sync = new object();
    private Thread listener;
    private NamedPipeServerStream currentPipe;
    private Process currentWorker;
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
        listener = new Thread(Listen) { IsBackground = true, Name = "TaskFolderMcpPipe" };
        listener.Start();
        Log("service started");
    }

    protected override void OnStop()
    {
        stopping = true;
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
            ProtectedRootAcl.Repair(config.ConfigPath);
            var input = reader.ReadLineAsync();
            if (!input.Wait(30000)) throw new System.TimeoutException("Client timed out.");
            var request = input.Result;
            if (request == null || Encoding.UTF8.GetByteCount(request) > 1024 * 1024) throw new InvalidDataException("Invalid request size.");
            var worker = new Process();
            worker.StartInfo = new ProcessStartInfo(config.NodePath,
                "\"" + Path.Combine(config.InstallRoot, "folder-worker.js") + "\" \"" + config.ConfigPath + "\"") {
                WorkingDirectory = config.InstallRoot, UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true
            };
            lock (sync) currentWorker = worker;
            worker.Start();
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
            writer.WriteLine(response);
        } catch (Exception error) {
            Log("request error: " + error.GetType().Name + ": " + error.Message);
            try { writer.WriteLine("{\"ok\":false,\"code\":\"SERVICE_ERROR\"}"); } catch (IOException) { }
        } finally {
            lock (sync) currentWorker = null;
            writer.Dispose();
            reader.Dispose();
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
