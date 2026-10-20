using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Collections.Generic;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

internal static class AppContainerProbe
{
    private const uint ExtendedStartupInfoPresent = 0x00080000;
    private const uint CreateSuspended = 0x00000004;
    private const uint CreateNoWindow = 0x08000000;
    private const uint ProcessTerminate = 0x0001;
    private const int TokenIsAppContainer = 29;
    private static readonly IntPtr SecurityCapabilitiesAttribute = new IntPtr(0x00020009);
    private static readonly IntPtr JobListAttribute = new IntPtr(0x0002000d);

    [StructLayout(LayoutKind.Sequential)]
    private struct SecurityCapabilities
    {
        internal IntPtr AppContainerSid;
        internal IntPtr Capabilities;
        internal int CapabilityCount;
        internal int Reserved;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct SidAndAttributes
    {
        internal IntPtr Sid;
        internal uint Attributes;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct StartupInfo
    {
        internal int cb;
        internal string lpReserved;
        internal string lpDesktop;
        internal string lpTitle;
        internal int dwX;
        internal int dwY;
        internal int dwXSize;
        internal int dwYSize;
        internal int dwXCountChars;
        internal int dwYCountChars;
        internal int dwFillAttribute;
        internal int dwFlags;
        internal short wShowWindow;
        internal short cbReserved2;
        internal IntPtr lpReserved2;
        internal IntPtr hStdInput;
        internal IntPtr hStdOutput;
        internal IntPtr hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct StartupInfoEx
    {
        internal StartupInfo StartupInfo;
        internal IntPtr AttributeList;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ProcessInformation
    {
        internal IntPtr Process;
        internal IntPtr Thread;
        internal int ProcessId;
        internal int ThreadId;
    }

    [DllImport("userenv.dll", CharSet = CharSet.Unicode)]
    private static extern int CreateAppContainerProfile(string name, string displayName, string description,
        IntPtr capabilities, int capabilityCount, out IntPtr sid);

    [DllImport("userenv.dll", CharSet = CharSet.Unicode)]
    private static extern int DeleteAppContainerProfile(string name);

    [DllImport("advapi32.dll")]
    private static extern IntPtr FreeSid(IntPtr sid);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool InitializeProcThreadAttributeList(IntPtr list, int count, int flags, ref IntPtr size);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool UpdateProcThreadAttribute(IntPtr list, uint flags, IntPtr attribute,
        IntPtr value, IntPtr size, IntPtr previousValue, IntPtr returnSize);

    [DllImport("kernel32.dll")]
    private static extern void DeleteProcThreadAttributeList(IntPtr list);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateProcess(string applicationName, StringBuilder commandLine,
        IntPtr processAttributes, IntPtr threadAttributes, bool inheritHandles, uint creationFlags,
        IntPtr environment, string currentDirectory, ref StartupInfoEx startupInfo,
        out ProcessInformation processInformation);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateJobObject(IntPtr job, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint ResumeThread(IntPtr thread);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint access, bool inheritHandle, int processId);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateFile(string path, uint access, uint sharing, IntPtr security,
        uint creation, uint flags, IntPtr template);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetFinalPathNameByHandle(IntPtr handle, StringBuilder path, uint length, uint flags);

    [DllImport("shell32.dll")]
    private static extern int SHGetKnownFolderPath(ref Guid folderId, uint flags, IntPtr token, out IntPtr path);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool OpenProcessToken(IntPtr process, uint desiredAccess, out IntPtr token);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool GetTokenInformation(IntPtr token, int informationClass,
        out int information, int informationLength, out int returnLength);

    private sealed class SandboxedProcess : IDisposable
    {
        internal readonly ProcessInformation Info;
        internal readonly IntPtr Job;

        internal SandboxedProcess(ProcessInformation info, IntPtr job) { Info = info; Job = job; }

        public void Dispose()
        {
            TerminateJobObject(Job, 1);
            CloseHandle(Info.Thread);
            CloseHandle(Info.Process);
            CloseHandle(Job);
        }
    }

    private static int Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "hold") { Thread.Sleep(15000); return 0; }
        if (args.Length == 4 && args[0] == "client") return RunAppServerClient(args[1], args[2], args[3]);
        if (args.Length == 2 && args[0] == "session-create")
            return TryFileAction(() => File.WriteAllText(args[1], "A"));
        if (args.Length == 2 && args[0] == "session-append")
            return TryFileAction(() => File.AppendAllText(args[1], "B"));
        if (args.Length == 2 && args[0] == "session-delete")
            return TryFileAction(() => File.Delete(args[1]));
        if (args.Length == 2 && args[0] == "network") return CheckNetwork(args[1]);
        if (args.Length == 3 && args[0] == "session-chmod") return TryFileAction(() => {
            var acl = File.GetAccessControl(args[1]);
            acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(args[2]), FileSystemRights.Modify,
                AccessControlType.Allow));
            File.SetAccessControl(args[1], acl);
        });
        if ((args.Length == 3 || args.Length == 4) && args[0] == "deny") {
            foreach (var value in new[] { args[1], args[2] }) {
                var handle = OpenProcess(ProcessTerminate, false, int.Parse(value));
                if (handle != IntPtr.Zero) { CloseHandle(handle); return 1; }
                if (Marshal.GetLastWin32Error() != 5) return 2;
            }
            if (args.Length == 4) {
                try { using (File.OpenRead(args[3])) return 4; }
                catch (UnauthorizedAccessException) { }
            }
            return 0;
        }
        bool realTurn = (args.Length == 3 || args.Length == 4) && args[2] == "turn";
        Guid suppliedId;
        if (realTurn && args.Length == 4 &&
            (!args[3].StartsWith("AIDEV57.A.", StringComparison.Ordinal) ||
             !Guid.TryParseExact(args[3].Substring(10), "N", out suppliedId))) return 3;
        bool sharedAcl = args.Length == 2 &&
            (args[0] == "shared-acl" || args[0] == "create-only-acl") && Directory.Exists(args[1]);
        if (!sharedAcl && ((args.Length > 2 && !realTurn) || (args.Length >= 1 && !File.Exists(args[0])) ||
            (args.Length == 2 && !File.Exists(args[1])))) return 3;
        if (!sharedAcl && args.Length >= 2) using (File.OpenRead(args[1])) { }

        var exe = System.Reflection.Assembly.GetExecutingAssembly().Location;
        var nameA = realTurn && args.Length == 4 ? args[3] : "AIDEV57.A." + Guid.NewGuid().ToString("N");
        var nameB = "AIDEV57.B." + Guid.NewGuid().ToString("N");
        IntPtr sidA = IntPtr.Zero, sidB = IntPtr.Zero;
        string profilePath = null;
        string codexHome = null;
        SecurityIdentifier profileAccessSid = null;
        int result = 1;
        try {
            CheckHr("CreateAppContainerProfile A", CreateAppContainerProfile(nameA, nameA, "Task Tracker isolation probe", IntPtr.Zero, 0, out sidA));
            CheckHr("CreateAppContainerProfile B", CreateAppContainerProfile(nameB, nameB, "Task Tracker isolation probe", IntPtr.Zero, 0, out sidB));
            GrantExecute(exe, new SecurityIdentifier(sidA));
            GrantExecute(exe, new SecurityIdentifier(sidB));
            using (var other = Start(exe, sidB, "hold")) {
                VerifyToken(other.Info.Process);
                if (ResumeThread(other.Info.Thread) == UInt32.MaxValue) throw LastError("ResumeThread B");
                var deny = "deny " + other.Info.ProcessId + " " + Process.GetCurrentProcess().Id;
                if (!sharedAcl && args.Length >= 2) deny += " \"" + args[1] + "\"";
                using (var probe = Start(exe, sidA, deny)) {
                    VerifyToken(probe.Info.Process);
                    if (ResumeThread(probe.Info.Thread) == UInt32.MaxValue) throw LastError("ResumeThread A");
                    if (WaitForSingleObject(probe.Info.Process, 10000) != 0) throw new Exception("Probe did not finish.");
                    uint code;
                    if (!GetExitCodeProcess(probe.Info.Process, out code) || code != 0)
                        throw new Exception("AppContainer could access another process or probe failed: " + code);
                }
            }
            if (sharedAcl) {
                var directory = Path.GetFullPath(args[1]);
                var session = Path.Combine(directory, "rollout-probe.jsonl");
                if (args[0] == "shared-acl") {
                    GrantModify(directory, new SecurityIdentifier(sidA));
                    GrantModify(directory, new SecurityIdentifier(sidB));
                    RunChild(exe, sidA, "session-create \"" + session + "\"");
                    RunChild(exe, sidB, "session-append \"" + session + "\"");
                    if (File.ReadAllText(session) != "AB") throw new Exception("Sibling did not append to a newly created session file.");
                    RunChild(exe, sidB, "session-delete \"" + session + "\"");
                    if (File.Exists(session)) throw new Exception("Sibling did not delete the session file.");
                    Console.WriteLine("Shared inherited Modify allows sibling AppContainer to alter and delete a new file.");
                } else {
                    GrantCreateOnly(directory, new SecurityIdentifier(sidA));
                    GrantCreateOnly(directory, new SecurityIdentifier(sidB));
                    var created = RunChildStatus(exe, sidA, "session-create \"" + session + "\"");
                    if (created != 0 || !File.Exists(session))
                        throw new Exception("Create-only: A could not create/write a new file; exit=" + created);
                    else {
                        var ownBefore = RunChildStatus(exe, sidA, "session-append \"" + session + "\"");
                        var otherBefore = RunChildStatus(exe, sidB, "session-append \"" + session + "\"");
                        var chmodBefore = RunChildStatus(exe, sidB, "session-chmod \"" + session + "\" " +
                            new SecurityIdentifier(sidB).Value);
                        GrantFileModify(session, new SecurityIdentifier(sidA));
                        var ownAfter = RunChildStatus(exe, sidA, "session-append \"" + session + "\"");
                        var otherAfter = RunChildStatus(exe, sidB, "session-append \"" + session + "\"");
                        var delete = RunChildStatus(exe, sidB, "session-delete \"" + session + "\"");
                        if (ownBefore != 5 || otherBefore != 5 || chmodBefore != 5 ||
                            ownAfter != 0 || otherAfter != 5 || delete != 5 || File.ReadAllText(session) != "AB")
                            throw new Exception("Create-only ACL did not enforce the expected handoff.");
                        Console.WriteLine("Create-only: A reopen before=" + ownBefore + ", B append before=" +
                            otherBefore + ", B chmod before=" + chmodBefore + ", A reopen after=" +
                            ownAfter + ", B append after=" + otherAfter + ", B delete=" + delete +
                            ", content=" + File.ReadAllText(session));
                    }
                }
            }
            if (!sharedAcl && args.Length >= 1) {
                GrantExecute(args[0], new SecurityIdentifier(sidA));
                using (var codex = Start(args[0], sidA, "--version")) {
                    VerifyToken(codex.Info.Process);
                    if (ResumeThread(codex.Info.Thread) == UInt32.MaxValue) throw LastError("ResumeThread Codex");
                    if (WaitForSingleObject(codex.Info.Process, 30000) != 0) throw new Exception("Codex did not finish.");
                    uint code;
                    if (!GetExitCodeProcess(codex.Info.Process, out code) || code != 0)
                        throw new Exception("Codex failed in AppContainer: " + code);
                }
                Console.WriteLine("Codex --version ran in AppContainer.");
                if (realTurn) {
                    var resultPath = Path.Combine(Path.GetDirectoryName(exe), "appserver-result.txt");
                    GrantModify(Path.GetDirectoryName(exe), new SecurityIdentifier(sidA));
                    profilePath = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
                    codexHome = Path.Combine(profilePath, ".codex");
                    if (!Directory.Exists(codexHome)) throw new Exception("Native Codex home does not exist: " + codexHome);
                    profileAccessSid = new SecurityIdentifier(sidA);
                    GrantProfileAccess(profilePath, codexHome, profileAccessSid);
                    var networkPath = Path.Combine(Path.GetDirectoryName(exe), "network-result.txt");
                    var networkCode = RunChildStatus(exe, sidA, "network \"" + networkPath + "\"");
                    if (networkCode != 0) throw new Exception("AppContainer network check: " + File.ReadAllText(networkPath));
                    using (var client = Start(exe, sidA,
                        "client \"" + args[0] + "\" \"" + resultPath + "\" \"" + profilePath + "\"")) {
                        VerifyToken(client.Info.Process);
                        if (ResumeThread(client.Info.Thread) == UInt32.MaxValue) throw LastError("ResumeThread client");
                        if (WaitForSingleObject(client.Info.Process, 180000) != 0) throw new Exception("App-server client timed out.");
                        uint code;
                        if (!GetExitCodeProcess(client.Info.Process, out code) || code != 0)
                            throw new Exception("App-server turn in AppContainer failed: " +
                                (File.Exists(resultPath) ? File.ReadAllText(resultPath) : "exit " + code));
                    }
                    Console.WriteLine("Codex app-server turn completed and persisted session resumed in AppContainer.");
                }
            }
            Console.WriteLine("Two AppContainer tokens under " + WindowsIdentity.GetCurrent().User.Value +
                " could not open the sibling or parent with PROCESS_TERMINATE.");
            if (!sharedAcl && args.Length >= 2) Console.WriteLine("AppContainer could not read the protected file.");
            result = 0;
        } catch (Exception error) {
            Console.Error.WriteLine(error.Message);
        } finally {
            if (profileAccessSid != null) {
                try { RevokeProfileAccess(profilePath, codexHome, profileAccessSid); }
                catch (Exception error) { Console.Error.WriteLine("Revoke profile ACL: " + error.Message); result = 1; }
            }
            if (sidA != IntPtr.Zero) {
                FreeSid(sidA);
                var status = DeleteAppContainerProfile(nameA);
                if (status < 0) { Console.Error.WriteLine("DeleteAppContainerProfile A: 0x" + status.ToString("X8")); result = 1; }
            }
            if (sidB != IntPtr.Zero) {
                FreeSid(sidB);
                var status = DeleteAppContainerProfile(nameB);
                if (status < 0) { Console.Error.WriteLine("DeleteAppContainerProfile B: 0x" + status.ToString("X8")); result = 1; }
            }
        }
        return result;
    }

    private static void CheckHr(string operation, int result)
    {
        if (result < 0) throw new Exception(operation + ": 0x" + result.ToString("X8"));
    }

    private static int TryFileAction(Action action)
    {
        try { action(); return 0; }
        catch (UnauthorizedAccessException) { return 5; }
    }

    private static int CheckNetwork(string resultPath)
    {
        var proxy = Environment.GetEnvironmentVariable("HTTPS_PROXY");
        Uri proxyUri;
        var proxyEndpoint = Uri.TryCreate(proxy, UriKind.Absolute, out proxyUri)
            ? proxyUri.Host + ":" + proxyUri.Port : (String.IsNullOrEmpty(proxy) ? "none" : "set");
        var host = proxyUri == null ? "chatgpt.com" : proxyUri.Host;
        var port = proxyUri == null ? 443 : proxyUri.Port;
        try {
            using (var client = new TcpClient()) {
                var connect = client.BeginConnect(host, port, null, null);
                if (!connect.AsyncWaitHandle.WaitOne(8000)) throw new TimeoutException("TCP timed out");
                client.EndConnect(connect);
            }
            File.WriteAllText(resultPath, "TCP to " + host + ":" + port + " succeeded; HTTPS_PROXY=" + proxyEndpoint);
            return 0;
        } catch (Exception error) {
            File.WriteAllText(resultPath, "TCP to " + host + ":" + port + " failed: " + error.GetType().Name +
                " " + error.Message + "; HTTPS_PROXY=" + proxyEndpoint);
            return 1;
        }
    }

    private static Exception LastError(string operation)
    {
        return new Win32Exception(Marshal.GetLastWin32Error(), operation);
    }

    private static void GrantExecute(string exe, SecurityIdentifier sid)
    {
        var directory = Path.GetDirectoryName(exe);
        var directoryAcl = Directory.GetAccessControl(directory);
        directoryAcl.AddAccessRule(new FileSystemAccessRule(sid, FileSystemRights.ReadAndExecute,
            InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
            PropagationFlags.None, AccessControlType.Allow));
        Directory.SetAccessControl(directory, directoryAcl);
        var fileAcl = File.GetAccessControl(exe);
        fileAcl.AddAccessRule(new FileSystemAccessRule(sid, FileSystemRights.ReadAndExecute, AccessControlType.Allow));
        File.SetAccessControl(exe, fileAcl);
    }

    private static void GrantModify(string directory, SecurityIdentifier sid)
    {
        var acl = Directory.GetAccessControl(directory);
        acl.AddAccessRule(new FileSystemAccessRule(sid, FileSystemRights.Modify,
            InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
            PropagationFlags.None, AccessControlType.Allow));
        Directory.SetAccessControl(directory, acl);
    }

    private static void GrantProfileAccess(string profile, string home, SecurityIdentifier sid)
    {
        // T1 only: bootstrap/Tracker must perform these ACL changes in production.
        var profileAcl = Directory.GetAccessControl(profile);
        profileAcl.AddAccessRule(new FileSystemAccessRule(sid,
            FileSystemRights.ReadAttributes | FileSystemRights.Traverse, AccessControlType.Allow));
        Directory.SetAccessControl(profile, profileAcl);
        GrantModify(home, sid);
    }

    private static void RevokeProfileAccess(string profile, string home, SecurityIdentifier sid)
    {
        try {
            var homeAcl = Directory.GetAccessControl(home);
            homeAcl.RemoveAccessRuleSpecific(new FileSystemAccessRule(sid, FileSystemRights.Modify,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                PropagationFlags.None, AccessControlType.Allow));
            Directory.SetAccessControl(home, homeAcl);
        } finally {
            var profileAcl = Directory.GetAccessControl(profile);
            profileAcl.RemoveAccessRuleSpecific(new FileSystemAccessRule(sid,
                FileSystemRights.ReadAttributes | FileSystemRights.Traverse, AccessControlType.Allow));
            Directory.SetAccessControl(profile, profileAcl);
        }
    }

    private static void GrantCreateOnly(string directory, SecurityIdentifier sid)
    {
        var acl = Directory.GetAccessControl(directory);
        acl.AddAccessRule(new FileSystemAccessRule(sid, FileSystemRights.CreateFiles,
            InheritanceFlags.None, PropagationFlags.None, AccessControlType.Allow));
        Directory.SetAccessControl(directory, acl);
    }

    private static void GrantFileModify(string file, SecurityIdentifier sid)
    {
        var acl = File.GetAccessControl(file);
        acl.AddAccessRule(new FileSystemAccessRule(sid, FileSystemRights.Modify, AccessControlType.Allow));
        File.SetAccessControl(file, acl);
    }

    private static void RunChild(string exe, IntPtr sid, string arguments)
    {
        var code = RunChildStatus(exe, sid, arguments);
        if (code != 0) throw new Exception("Shared ACL child failed: " + code);
    }

    private static uint RunChildStatus(string exe, IntPtr sid, string arguments)
    {
        using (var child = Start(exe, sid, arguments)) {
            VerifyToken(child.Info.Process);
            if (ResumeThread(child.Info.Thread) == UInt32.MaxValue) throw LastError("ResumeThread shared ACL probe");
            if (WaitForSingleObject(child.Info.Process, 10000) != 0) throw new Exception("Shared ACL child did not finish.");
            uint code;
            if (!GetExitCodeProcess(child.Info.Process, out code)) throw LastError("GetExitCodeProcess shared ACL probe");
            return code;
        }
    }

    private static int RunAppServerClient(string codex, string resultPath, string profilePath)
    {
        var errorOutput = new StringBuilder();
        try {
            var root = Path.GetDirectoryName(resultPath);
            errorOutput.AppendLine("working directory exists: " + Directory.Exists(root));
            errorOutput.AppendLine("working directory entries: " + Directory.GetFileSystemEntries(root).Length);
        } catch (Exception accessError) { errorOutput.AppendLine("working directory listing: " + accessError.Message); }
        try {
            var start = new ProcessStartInfo(codex, "app-server --stdio") {
                UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
                WorkingDirectory = Path.GetDirectoryName(resultPath)
            };
            var home = Path.Combine(profilePath, ".codex");
            errorOutput.AppendLine("Native Codex home: " + home);
            var profileFolderId = new Guid("5E6C858F-0E22-4760-9AFE-EA3317B67173");
            IntPtr knownProfile;
            var knownFolderStatus = SHGetKnownFolderPath(ref profileFolderId, 0, IntPtr.Zero, out knownProfile);
            errorOutput.AppendLine("FOLDERID_Profile: 0x" + knownFolderStatus.ToString("X8") +
                (knownProfile == IntPtr.Zero ? "" : " " + Marshal.PtrToStringUni(knownProfile)));
            if (knownProfile != IntPtr.Zero) Marshal.FreeCoTaskMem(knownProfile);
            knownFolderStatus = SHGetKnownFolderPath(ref profileFolderId, 0x00004000, IntPtr.Zero, out knownProfile);
            errorOutput.AppendLine("FOLDERID_Profile without verification: 0x" + knownFolderStatus.ToString("X8") +
                (knownProfile == IntPtr.Zero ? "" : " " + Marshal.PtrToStringUni(knownProfile)));
            if (knownProfile != IntPtr.Zero) Marshal.FreeCoTaskMem(knownProfile);
            var directoryHandle = CreateFile(home, 0x80, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero);
            if (directoryHandle == new IntPtr(-1)) errorOutput.AppendLine("Open native Codex home: " + Marshal.GetLastWin32Error());
            else {
                try {
                    var resolved = new StringBuilder(1024);
                    var length = GetFinalPathNameByHandle(directoryHandle, resolved, (uint)resolved.Capacity, 0);
                    errorOutput.AppendLine("Normalize native Codex home: " + (length == 0 ? "error " + Marshal.GetLastWin32Error() : resolved.ToString()));
                    length = GetFinalPathNameByHandle(directoryHandle, resolved, (uint)resolved.Capacity, 8);
                    errorOutput.AppendLine("Opened native Codex home path: " + (length == 0 ? "error " + Marshal.GetLastWin32Error() : resolved.ToString()));
                    length = GetFinalPathNameByHandle(directoryHandle, resolved, (uint)resolved.Capacity, 2);
                    errorOutput.AppendLine("NT native Codex home path: " + (length == 0 ? "error " + Marshal.GetLastWin32Error() : resolved.ToString()));
                } finally { CloseHandle(directoryHandle); }
            }
            start.EnvironmentVariables["HOME"] = profilePath;
            start.EnvironmentVariables["USERPROFILE"] = profilePath;
            start.EnvironmentVariables.Remove("CODEX_HOME");
            string threadId;
            using (var process = Process.Start(start)) {
                if (process == null) throw new Exception("Could not start Codex app-server.");
                VerifyToken(process.Handle);
                process.ErrorDataReceived += (sender, eventArgs) => {
                    if (eventArgs.Data != null && errorOutput.Length < 8000) errorOutput.AppendLine(eventArgs.Data);
                };
                process.BeginErrorReadLine();
                try {
                    var json = new JavaScriptSerializer();
                    Send(process, json, 1, "initialize", new Dictionary<string, object> {
                        { "clientInfo", new Dictionary<string, string> { { "name", "aidev57-probe" }, { "version", "0.1.0" } } }
                    });
                    Receive(process, json, 1, null);
                    process.StandardInput.WriteLine("{\"method\":\"initialized\",\"params\":{}}");
                    Send(process, json, 2, "thread/start", new Dictionary<string, object> {
                        { "cwd", Path.GetDirectoryName(resultPath) }, { "ephemeral", false }, { "model", "gpt-5.6-sol" }
                    });
                    var thread = Field(Field(Receive(process, json, 2, null), "result"), "thread");
                    threadId = (string)Field(thread, "id");
                    Send(process, json, 3, "turn/start", new Dictionary<string, object> {
                        { "threadId", threadId }, { "input", new object[] {
                            new Dictionary<string, string> { { "type", "text" }, { "text", "Ответь одним словом: OK" } }
                        } }
                    });
                    string completed = null;
                    var turn = Field(Field(Receive(process, json, 3, line => {
                        if ((string)Field(line, "method") == "turn/completed") completed = json.Serialize(line);
                    }), "result"), "turn");
                    var turnId = (string)Field(turn, "id");
                    var deadline = DateTime.UtcNow.AddSeconds(90);
                    while (completed == null && DateTime.UtcNow < deadline) {
                        var line = ReadLine(process, 90);
                        var message = json.DeserializeObject(line) as Dictionary<string, object>;
                        if (message != null && (string)Field(message, "method") == "turn/completed") completed = line;
                    }
                    if (completed == null) throw new Exception("No turn/completed event.");
                    var finish = Field(Field(json.DeserializeObject(completed), "params"), "turn");
                    if ((string)Field(finish, "id") != turnId || (string)Field(finish, "status") != "completed")
                        throw new Exception("Turn result: " + completed);
                } finally {
                    if (!process.HasExited) process.Kill();
                    if (!process.WaitForExit(5000)) throw new Exception("First Codex app-server did not exit.");
                }
            }
            using (var process = Process.Start(start)) {
                if (process == null) throw new Exception("Could not restart Codex app-server.");
                VerifyToken(process.Handle);
                process.ErrorDataReceived += (sender, eventArgs) => {
                    if (eventArgs.Data != null && errorOutput.Length < 8000) errorOutput.AppendLine(eventArgs.Data);
                };
                process.BeginErrorReadLine();
                try {
                    var json = new JavaScriptSerializer();
                    Send(process, json, 1, "initialize", new Dictionary<string, object> {
                        { "clientInfo", new Dictionary<string, string> { { "name", "aidev57-probe" }, { "version", "0.1.0" } } }
                    });
                    Receive(process, json, 1, null);
                    process.StandardInput.WriteLine("{\"method\":\"initialized\",\"params\":{}}");
                    Send(process, json, 2, "thread/resume", new Dictionary<string, object> { { "threadId", threadId } });
                    var thread = Field(Field(Receive(process, json, 2, null), "result"), "thread");
                    var turns = Field(thread, "turns") as object[];
                    if ((string)Field(thread, "id") != threadId || turns == null || turns.Length == 0)
                        throw new Exception("Persisted thread did not resume with its completed turn.");
                    File.WriteAllText(resultPath, "resumed " + threadId + " turns=" + turns.Length);
                    return 0;
                } finally {
                    if (!process.HasExited) process.Kill();
                }
            }
        } catch (Exception error) {
            try { File.WriteAllText(resultPath, error + Environment.NewLine + errorOutput); } catch { }
            return 1;
        }
    }

    private static object Field(object value, string name)
    {
        var fields = value as Dictionary<string, object>;
        object result;
        return fields != null && fields.TryGetValue(name, out result) ? result : null;
    }

    private static void Send(Process process, JavaScriptSerializer json, int id, string method, object parameters)
    {
        process.StandardInput.WriteLine(json.Serialize(new Dictionary<string, object> {
            { "id", id }, { "method", method }, { "params", parameters }
        }));
        process.StandardInput.Flush();
    }

    private static Dictionary<string, object> Receive(Process process, JavaScriptSerializer json, int id,
        Action<Dictionary<string, object>> notification)
    {
        for (;;) {
            var line = ReadLine(process, 30);
            var message = json.DeserializeObject(line) as Dictionary<string, object>;
            if (message == null) continue;
            if (Field(message, "id") is int && (int)Field(message, "id") == id) {
                if (Field(message, "error") != null) throw new Exception("Request " + id + ": " + line);
                return message;
            }
            if (notification != null) notification(message);
        }
    }

    private static string ReadLine(Process process, int seconds)
    {
        var read = process.StandardOutput.ReadLineAsync();
        if (!read.Wait(TimeSpan.FromSeconds(seconds))) throw new TimeoutException("Codex app-server response timed out.");
        if (read.Result == null) throw new Exception("Codex app-server exited: " + process.ExitCode);
        return read.Result;
    }

    private static SandboxedProcess Start(string exe, IntPtr sid, string arguments)
    {
        var job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) throw LastError("CreateJobObject");
        IntPtr list = IntPtr.Zero, capabilities = IntPtr.Zero, networkCapability = IntPtr.Zero;
        IntPtr networkSid = IntPtr.Zero, jobValue = IntPtr.Zero;
        bool initialized = false;
        try {
            IntPtr size = IntPtr.Zero;
            InitializeProcThreadAttributeList(IntPtr.Zero, 2, 0, ref size);
            list = Marshal.AllocHGlobal(size);
            if (!InitializeProcThreadAttributeList(list, 2, 0, ref size)) throw LastError("InitializeProcThreadAttributeList");
            initialized = true;
            var internetClient = new SecurityIdentifier("S-1-15-3-1");
            var sidBytes = new byte[internetClient.BinaryLength];
            internetClient.GetBinaryForm(sidBytes, 0);
            networkSid = Marshal.AllocHGlobal(sidBytes.Length);
            Marshal.Copy(sidBytes, 0, networkSid, sidBytes.Length);
            networkCapability = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(SidAndAttributes)));
            Marshal.StructureToPtr(new SidAndAttributes { Sid = networkSid, Attributes = 4 }, networkCapability, false);
            capabilities = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(SecurityCapabilities)));
            Marshal.StructureToPtr(new SecurityCapabilities {
                AppContainerSid = sid, Capabilities = networkCapability, CapabilityCount = 1
            }, capabilities, false);
            if (!UpdateProcThreadAttribute(list, 0, SecurityCapabilitiesAttribute, capabilities,
                    new IntPtr(Marshal.SizeOf(typeof(SecurityCapabilities))), IntPtr.Zero, IntPtr.Zero))
                throw LastError("UpdateProcThreadAttribute security");
            jobValue = Marshal.AllocHGlobal(IntPtr.Size);
            Marshal.WriteIntPtr(jobValue, job);
            if (!UpdateProcThreadAttribute(list, 0, JobListAttribute, jobValue,
                    new IntPtr(IntPtr.Size), IntPtr.Zero, IntPtr.Zero))
                throw LastError("UpdateProcThreadAttribute job");
            var startup = new StartupInfoEx { AttributeList = list };
            startup.StartupInfo.cb = Marshal.SizeOf(typeof(StartupInfoEx));
            ProcessInformation info;
            var command = new StringBuilder("\"" + exe + "\" " + arguments);
            if (!CreateProcess(exe, command, IntPtr.Zero, IntPtr.Zero, false,
                    ExtendedStartupInfoPresent | CreateSuspended | CreateNoWindow,
                    IntPtr.Zero, Path.GetDirectoryName(exe), ref startup, out info))
                throw LastError("CreateProcess");
            return new SandboxedProcess(info, job);
        } catch {
            CloseHandle(job);
            throw;
        } finally {
            if (initialized) DeleteProcThreadAttributeList(list);
            if (list != IntPtr.Zero) Marshal.FreeHGlobal(list);
            if (capabilities != IntPtr.Zero) Marshal.FreeHGlobal(capabilities);
            if (networkCapability != IntPtr.Zero) Marshal.FreeHGlobal(networkCapability);
            if (networkSid != IntPtr.Zero) Marshal.FreeHGlobal(networkSid);
            if (jobValue != IntPtr.Zero) Marshal.FreeHGlobal(jobValue);
        }
    }

    private static void VerifyToken(IntPtr process)
    {
        IntPtr token;
        if (!OpenProcessToken(process, 0x0008, out token)) throw LastError("OpenProcessToken");
        try {
            int isAppContainer, length;
            if (!GetTokenInformation(token, TokenIsAppContainer, out isAppContainer, sizeof(int), out length))
                throw LastError("GetTokenInformation");
            if (isAppContainer != 1) throw new Exception("Child process is not in AppContainer.");
            using (var identity = new WindowsIdentity(token)) {
                if (identity.User != WindowsIdentity.GetCurrent().User)
                    throw new Exception("Child process has the wrong user SID.");
            }
        } finally { CloseHandle(token); }
    }
}
