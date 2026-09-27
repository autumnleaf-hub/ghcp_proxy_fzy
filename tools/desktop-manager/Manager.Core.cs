using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Web.Script.Serialization;

namespace BpsManager
{
    internal static class Paths
    {
        public static string Normalize(string value)
        {
            if (String.IsNullOrWhiteSpace(value) || !Path.IsPathRooted(value) ||
                !(value.StartsWith(@"\\") || (value.Length > 2 && value[1] == ':' && (value[2] == '\\' || value[2] == '/'))))
                throw new InvalidDataException("项目目录必须是绝对路径。");
            return Path.GetFullPath(value).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        }
        public static bool Same(string a, string b)
        {
            try { return String.Equals(Normalize(a), Normalize(b), StringComparison.OrdinalIgnoreCase); }
            catch { return false; }
        }
        public static string ProjectKey(string root)
        {
            using (SHA256 hash = SHA256.Create())
                return BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(Normalize(root).ToUpperInvariant()))).Replace("-", "").Substring(0, 24);
        }
        // CommandLineToArgvW-compatible quoting, including trailing backslashes.
        public static string Quote(string arg)
        {
            StringBuilder b = new StringBuilder("\""); int slashes = 0;
            foreach (char c in arg)
            {
                if (c == '\\') { slashes++; continue; }
                if (c == '"') { b.Append('\\', slashes * 2 + 1); b.Append(c); slashes = 0; continue; }
                b.Append('\\', slashes); slashes = 0; b.Append(c);
            }
            b.Append('\\', slashes * 2); b.Append('"'); return b.ToString();
        }
        public static string AutoStartCommand(string exe, string root)
        { return Quote(exe) + " --project " + Quote(root) + " --autostart"; }
    }

    internal sealed class Identity
    {
        public string service { get; set; }
        public string project_root { get; set; }
        public int pid { get; set; }
        public int port { get; set; }
        public string instance_id { get; set; }
        public double started_at { get; set; }
        public bool? graceful_stop_supported { get; set; }
        public bool SameInstance(Identity other)
        {
            return other != null && service == other.service && Paths.Same(project_root, other.project_root) &&
                pid == other.pid && port == other.port && instance_id == other.instance_id && started_at == other.started_at && graceful_stop_supported == other.graceful_stop_supported;
        }
    }
    internal sealed class ProcInfo
    {
        public int Pid; public string Executable; public string CommandLine; public string OwnerSid;
        public string OwnerName; public DateTime CreatedUtc; public string[] Args;
        public bool SameProcess(ProcInfo other)
        {
            return other != null && Pid == other.Pid && CreatedUtc == other.CreatedUtc &&
                OwnerSid == other.OwnerSid && OwnerName == other.OwnerName &&
                String.Equals(Executable, other.Executable, StringComparison.OrdinalIgnoreCase) && CommandLine == other.CommandLine;
        }
    }
    internal sealed class IdentityReply
    {
        public Identity Value; public bool Missing; public string Error;
    }
    internal interface IRuntime
    {
        int[] Listeners(int port);
        ProcInfo ReadProcess(int pid);
        IdentityReply ReadIdentity(int port);
        void GracefulStop(int port, Identity identity);
        void KillExact(ProcInfo process, int port);
        void Launch(string root, int port, string logDirectory);
    }
    internal enum ServiceKind { Stopped, Running, Legacy, Conflict, Unverified, Starting, Stopping }
    internal sealed class ServiceState
    {
        public ServiceKind Kind; public string Detail; public Identity Identity; public ProcInfo Process; public int Port;
        public bool CanStop { get { return Kind == ServiceKind.Running || Kind == ServiceKind.Legacy; } }
        public bool RequiresForceStop { get { return Kind == ServiceKind.Legacy || (Kind == ServiceKind.Running && Identity != null && Identity.graceful_stop_supported == false); } }
    }
    internal sealed class Controller
    {
        private readonly IRuntime runtime; private readonly string root, sid, owner;
        public Controller(IRuntime runtime, string root, string sid, string owner)
        { this.runtime = runtime; this.root = Paths.Normalize(root); this.sid = sid; this.owner = owner; }
        private bool ValidateProcess(ProcInfo p, bool allowRelative)
        {
            if (p == null || p.Pid <= 0 || p.OwnerSid != sid || !String.Equals(p.OwnerName, owner, StringComparison.OrdinalIgnoreCase) ||
                p.CreatedUtc.Kind != DateTimeKind.Utc || p.Args == null || p.Args.Length < 2) return false;
            string exe = Path.GetFileName(p.Executable ?? "");
            if (!String.Equals(exe, "python.exe", StringComparison.OrdinalIgnoreCase) &&
                !String.Equals(exe, "pythonw.exe", StringComparison.OrdinalIgnoreCase)) return false;
            // Accept Python flags; uvicorn -m requires verified identity. Never accept -c or another script.
            int script = 1;
            while (script < p.Args.Length)
            {
                string arg = p.Args[script];
                if ((arg == "-X" || arg == "-W") && script + 1 < p.Args.Length) { script += 2; continue; }
                if ((arg.StartsWith("-X") || arg.StartsWith("-W")) && arg.Length > 2) { script++; continue; }
                if (arg == "-m") return allowRelative && script + 2 < p.Args.Length && p.Args[script + 1] == "uvicorn" && p.Args[script + 2] == "proxy:app";
                if (arg == "-u" || arg == "-B" || arg == "-E" || arg == "-s" || arg == "-S" || arg == "-I" || arg == "-O" || arg == "-OO") { script++; continue; }
                break;
            }
            if (script >= p.Args.Length) return false;
            string path = p.Args[script];
            if (Paths.Same(path, Path.Combine(root, "proxy.py"))) return true;
            return allowRelative && (path == "proxy.py" || path == @".\proxy.py" || path == "./proxy.py");
        }
        public bool ValidateIdentity(Identity id, ProcInfo p, int port)
        {
            if (id == null || id.service != "ghcp_proxy" || !Paths.Same(id.project_root, root) || id.pid <= 0 ||
                p == null || id.pid != p.Pid || id.port != port || String.IsNullOrWhiteSpace(id.instance_id) ||
                id.instance_id.Length > 256 || Double.IsNaN(id.started_at) || Double.IsInfinity(id.started_at) || !ValidateProcess(p, true)) return false;
            double creation = (p.CreatedUtc - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
            double delta = id.started_at - creation;
            return delta >= -1.0 && delta <= 30.0;
        }
        public ServiceState Inspect(int port)
        {
            Settings.ValidatePort(port);
            ServiceState s = new ServiceState { Port = port, Kind = ServiceKind.Unverified };
            try
            {
                int[] listeners = runtime.Listeners(port).Distinct().ToArray();
                if (listeners.Length == 0) { s.Kind = ServiceKind.Stopped; s.Detail = "就绪，可启动本项目的代理服务。"; return s; }
                if (listeners.Length != 1) { s.Kind = ServiceKind.Conflict; s.Detail = "多个进程占用此端口，无法安全操作。"; return s; }
                ProcInfo process = runtime.ReadProcess(listeners[0]);
                IdentityReply reply = runtime.ReadIdentity(port);
                // Verify the listener and process did not change while the HTTP request ran.
                int[] after = runtime.Listeners(port).Distinct().ToArray();
                ProcInfo current = runtime.ReadProcess(listeners[0]);
                if (after.Length != 1 || after[0] != listeners[0] || process == null || !process.SameProcess(current))
                { s.Detail = "验证期间进程已改变，请重新检查。"; return s; }
                if (reply.Value != null)
                {
                    if (ValidateIdentity(reply.Value, process, port))
                    { s.Kind = ServiceKind.Running; s.Identity = reply.Value; s.Process = process; s.Detail = "已验证本项目 · 当前用户 · PID " + process.Pid; }
                    else { s.Kind = ServiceKind.Conflict; s.Detail = "端口属于其他服务或身份无法验证，已拒绝操作。"; }
                }
                else if (reply.Missing && ValidateProcess(process, false))
                { s.Kind = ServiceKind.Legacy; s.Process = process; s.Detail = "已通过绝对路径识别旧版服务，停止前需要确认。"; }
                else
                {
                    s.Kind = ServiceKind.Conflict;
                    s.Detail = reply.Missing ? "无法确认服务身份；旧版相对路径启动的服务需要手动重启升级一次。" : "服务身份不可用或无效，已拒绝操作。 " + reply.Error;
                }
            }
            catch (Exception ex) { s.Kind = ServiceKind.Unverified; s.Detail = "无法完成验证：" + ex.Message; }
            return s;
        }
        public void Start(int port, string logDirectory)
        {
            ServiceState s = Inspect(port);
            if (s.Kind == ServiceKind.Running || s.Kind == ServiceKind.Legacy) return; // adopt; no duplicate
            if (s.Kind != ServiceKind.Stopped) throw new InvalidOperationException(s.Detail);
            runtime.Launch(root, port, logDirectory);
        }
        public void Stop(ServiceState expected, bool legacyConfirmed)
        {
            if (expected == null || !expected.CanStop) throw new InvalidOperationException("没有可以安全停止的已验证服务。");
            ServiceState now = Inspect(expected.Port);
            if (!now.CanStop || now.Kind != expected.Kind || !expected.Process.SameProcess(now.Process))
                throw new InvalidOperationException("服务已改变，已拒绝过期的停止操作，请刷新后重试。");
            if (now.Kind == ServiceKind.Running)
            {
                if (!expected.Identity.SameInstance(now.Identity)) throw new InvalidOperationException("实例已改变，已拒绝过期的停止操作。");
                if (now.RequiresForceStop)
                {
                    if (!legacyConfirmed) throw new InvalidOperationException("此启动方式不支持正常退出，请明确确认后再停止。");
                    runtime.KillExact(now.Process, now.Port);
                }
                else runtime.GracefulStop(now.Port, now.Identity);
            }
            else
            {
                if (!legacyConfirmed) throw new InvalidOperationException("停止旧版服务需要明确确认。");
                runtime.KillExact(now.Process, now.Port);
            }
        }
    }
    internal sealed class SettingsData { public int port { get; set; } }
    internal sealed class Settings
    {
        public int Port = 8001; public string Warning; private readonly string file;
        public Settings(string file) { this.file = file; }
        public static void ValidatePort(int port) { if (port < 1 || port > 65535) throw new ArgumentOutOfRangeException("port", "端口必须介于 1 和 65535 之间。"); }
        public void Load()
        {
            if (!File.Exists(file)) return;
            try { SettingsData d = new JavaScriptSerializer().Deserialize<SettingsData>(File.ReadAllText(file)); ValidatePort(d.port); Port = d.port; }
            catch (Exception ex) { Warning = "无法读取已保存端口，暂用 8001，原配置已保留。 " + ex.Message; }
        }
        public void SavePort(int port)
        {
            ValidatePort(port); Directory.CreateDirectory(Path.GetDirectoryName(file));
            string temporary = file + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                File.WriteAllText(temporary, new JavaScriptSerializer().Serialize(new SettingsData { port = port }), Encoding.UTF8);
                if (File.Exists(file)) File.Replace(temporary, file, file + ".bak"); else File.Move(temporary, file);
                Port = port; Warning = null;
            }
            finally { if (File.Exists(temporary)) File.Delete(temporary); }
        }
    }
    internal interface IRunStore { string Read(string name); void Set(string name, string value); void Remove(string name); }
    internal sealed class AutoStart
    {
        private readonly IRunStore store; private readonly string key, command;
        public AutoStart(IRunStore store, string root, string exe)
        { this.store = store; key = "BPS-Manager-" + Paths.ProjectKey(root); command = Paths.AutoStartCommand(exe, root); }
        public bool Enabled { get { return String.Equals(store.Read(key), command, StringComparison.Ordinal); } }
        public bool HasDifferentCommand { get { string current = store.Read(key); return current != null && current != command; } }
        public void Set(bool enabled)
        {
            if (enabled) store.Set(key, command);
            else if (HasDifferentCommand) throw new InvalidOperationException("启动项已被其他程序修改，为安全起见不予删除。");
            else store.Remove(key);
        }
    }
}
