using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Management;
using System.Net;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using Microsoft.Win32;

namespace BpsManager
{
    internal static class Native
    {
        [DllImport("shell32.dll", SetLastError = true)] private static extern IntPtr CommandLineToArgvW([MarshalAs(UnmanagedType.LPWStr)] string cmd, out int argc);
        [DllImport("kernel32.dll")] private static extern IntPtr LocalFree(IntPtr p);
        [DllImport("iphlpapi.dll", SetLastError = true)] private static extern uint GetExtendedTcpTable(IntPtr table, ref int size, bool ordered, int family, int tableClass, uint reserved);
        [DllImport("kernel32.dll", SetLastError = true)] private static extern bool GetProcessTimes(IntPtr handle, out long creation, out long exit, out long kernel, out long user);
        public static string[] SplitCommand(string command)
        {
            if (String.IsNullOrWhiteSpace(command)) return new string[0];
            int count; IntPtr p = CommandLineToArgvW(command, out count);
            if (p == IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
            try
            {
                string[] args = new string[count];
                for (int i = 0; i < count; i++) args[i] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(p, i * IntPtr.Size));
                return args;
            }
            finally { LocalFree(p); }
        }
        public static DateTime CreationTime(IntPtr handle)
        {
            long c, e, k, u;
            if (!GetProcessTimes(handle, out c, out e, out k, out u)) throw new System.ComponentModel.Win32Exception();
            return DateTime.FromFileTimeUtc(c);
        }
        public static int[] Listeners(int port)
        {
            HashSet<int> result = new HashSet<int>();
            foreach (int family in new int[] { 2, 23 })
            {
                int size = 0; uint error = GetExtendedTcpTable(IntPtr.Zero, ref size, true, family, 3, 0);
                if (error != 122 && error != 0) throw new System.ComponentModel.Win32Exception((int)error);
                bool read = false;
                for (int attempt = 0; attempt < 3 && !read; attempt++)
                {
                    IntPtr memory = Marshal.AllocHGlobal(size);
                    try
                    {
                        error = GetExtendedTcpTable(memory, ref size, true, family, 3, 0);
                        if (error == 122) continue;
                        if (error != 0) throw new System.ComponentModel.Win32Exception((int)error);
                        int count = Marshal.ReadInt32(memory); int rowSize = family == 2 ? 24 : 56;
                        if (count < 0 || 4L + (long)count * rowSize > size) throw new InvalidDataException("Invalid TCP owner table.");
                        for (int i = 0; i < count; i++)
                        {
                            IntPtr row = IntPtr.Add(memory, 4 + i * rowSize); int offset = family == 2 ? 8 : 20;
                            int localPort = Marshal.ReadByte(row, offset) * 256 + Marshal.ReadByte(row, offset + 1);
                            if (localPort == port) result.Add(Marshal.ReadInt32(row, family == 2 ? 20 : 52));
                        }
                        read = true;
                    }
                    finally { Marshal.FreeHGlobal(memory); }
                }
                if (!read) throw new IOException("TCP listeners kept changing; retry verification.");
            }
            return result.ToArray();
        }
    }
    internal sealed class RegistryRunStore : IRunStore
    {
        private const string PathName = @"Software\Microsoft\Windows\CurrentVersion\Run";
        public string Read(string name) { using (RegistryKey key = Registry.CurrentUser.OpenSubKey(PathName, false)) return key == null ? null : key.GetValue(name) as string; }
        public void Set(string name, string value) { using (RegistryKey key = Registry.CurrentUser.CreateSubKey(PathName)) key.SetValue(name, value, RegistryValueKind.String); }
        public void Remove(string name) { using (RegistryKey key = Registry.CurrentUser.OpenSubKey(PathName, true)) if (key != null) key.DeleteValue(name, false); }
    }
    internal sealed class LoopbackHttp
    {
        public IdentityReply ReadIdentity(int port)
        {
            try
            {
                string body = Send(port, "/api/desktop/identity", null);
                Identity id = new JavaScriptSerializer().Deserialize<Identity>(body);
                return new IdentityReply { Value = id, Error = id == null ? "Empty identity response." : null };
            }
            catch (WebException ex)
            {
                HttpWebResponse response = ex.Response as HttpWebResponse;
                bool missing = response != null && response.StatusCode == HttpStatusCode.NotFound;
                if (response != null) response.Close();
                return new IdentityReply { Missing = missing, Error = ex.Message };
            }
            catch (Exception ex) { return new IdentityReply { Error = ex.Message }; }
        }
        public void Stop(int port, Identity identity)
        {
            string json = new JavaScriptSerializer().Serialize(new { instance_id = identity.instance_id, pid = identity.pid });
            string body = Send(port, "/api/desktop/stop", json);
            Dictionary<string, object> reply = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(body);
            if (reply == null || !reply.ContainsKey("stopping") || !(reply["stopping"] is bool) || !(bool)reply["stopping"])
                throw new IOException("Service did not acknowledge graceful shutdown. It has NOT been force-stopped.");
        }
        internal string Send(int port, string path, string json)
        {
            Settings.ValidatePort(port);
            string origin = "http://127.0.0.1:" + port;
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create(origin + path);
            request.Proxy = null; request.AllowAutoRedirect = false; request.KeepAlive = false;
            request.Timeout = 1800; request.ReadWriteTimeout = 1800; request.Method = json == null ? "GET" : "POST";
            request.Accept = "application/json"; request.Headers["Origin"] = origin;
            request.UseDefaultCredentials = false; request.ServicePoint.Expect100Continue = false;
            // Bound the whole transaction, not just each read, including slow/drip responses.
            using (Timer deadline = new Timer(delegate { request.Abort(); }, null, 2200, Timeout.Infinite))
            {
                if (json != null)
                {
                    byte[] bytes = Encoding.UTF8.GetBytes(json); request.ContentType = "application/json"; request.ContentLength = bytes.Length;
                    using (Stream stream = request.GetRequestStream()) stream.Write(bytes, 0, bytes.Length);
                }
                using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                {
                    if (response.StatusCode != HttpStatusCode.OK && !(json != null && response.StatusCode == HttpStatusCode.Accepted)) throw new IOException("Unexpected HTTP status " + (int)response.StatusCode + ". Redirects are not followed.");
                    using (StreamReader reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8))
                    {
                        char[] buffer = new char[32769]; int length = 0, n;
                        while (length < buffer.Length && (n = reader.Read(buffer, length, buffer.Length - length)) > 0) length += n;
                        if (length > 32768) throw new IOException("Identity response is too large.");
                        return new String(buffer, 0, length);
                    }
                }
            }
        }
    }
    internal sealed class WindowsRuntime : IRuntime
    {
        private readonly LoopbackHttp http = new LoopbackHttp();
        public int[] Listeners(int port) { return Native.Listeners(port); }
        public ProcInfo ReadProcess(int pid)
        {
            if (pid <= 0) return null;
            ConnectionOptions connection = new ConnectionOptions { Timeout = TimeSpan.FromSeconds(2), EnablePrivileges = false };
            ManagementScope scope = new ManagementScope("//./root/cimv2".Replace('/' , (char)92), connection);
            ObjectGetOptions options = new ObjectGetOptions { Timeout = TimeSpan.FromSeconds(2) };
            try
            {
                using (ManagementObject process = new ManagementObject(scope, new ManagementPath("Win32_Process.Handle='" + pid + "'"), options))
                {
                    process.Get();
                    InvokeMethodOptions callOptions = new InvokeMethodOptions { Timeout = TimeSpan.FromSeconds(2) };
                    using (ManagementBaseObject owner = process.InvokeMethod("GetOwner", null, callOptions))
                    using (ManagementBaseObject sid = process.InvokeMethod("GetOwnerSid", null, callOptions))
                    {
                        if (owner == null || sid == null || Convert.ToUInt32(owner["ReturnValue"]) != 0 || Convert.ToUInt32(sid["ReturnValue"]) != 0) return null;
                        string cmd = process["CommandLine"] as string; string created = process["CreationDate"] as string;
                        if (String.IsNullOrWhiteSpace(created)) return null;
                        return new ProcInfo { Pid = pid, Executable = process["ExecutablePath"] as string, CommandLine = cmd,
                            Args = Native.SplitCommand(cmd), OwnerSid = sid["Sid"] as string,
                            OwnerName = Convert.ToString(owner["Domain"]) + @"\" + Convert.ToString(owner["User"]),
                            CreatedUtc = ManagementDateTimeConverter.ToDateTime(created).ToUniversalTime() };
                    }
                }
            }
            catch (ManagementException ex) { if (ex.ErrorCode == ManagementStatus.NotFound) return null; throw; }
            catch (ArgumentException) { return null; }
        }
        public IdentityReply ReadIdentity(int port) { return http.ReadIdentity(port); }
        public void GracefulStop(int port, Identity identity) { http.Stop(port, identity); }
        public void KillExact(ProcInfo expected, int port)
        {
            // Holding the Process.Handle pins the kernel process object against PID reuse.
            using (Process process = Process.GetProcessById(expected.Pid))
            {
                IntPtr handle = process.Handle; DateTime created = Native.CreationTime(handle);
                // WMI stores microseconds; truncate only the extra 100ns precision.
                if (created.Ticks / 10 != expected.CreatedUtc.Ticks / 10) throw new InvalidOperationException("PID creation time changed. Refusing legacy stop.");
                ProcInfo current = ReadProcess(expected.Pid); int[] listeners = Listeners(port).Distinct().ToArray();
                if (!expected.SameProcess(current) || listeners.Length != 1 || listeners[0] != expected.Pid || process.HasExited)
                    throw new InvalidOperationException("Legacy process identity changed. Refusing stop.");
                process.Kill();
            }
        }
        public void Launch(string root, int port, string logDirectory)
        {
            string python = Path.Combine(root, @".venv\Scripts\python.exe"); string script = Path.Combine(root, "proxy.py");
            if (!File.Exists(python)) throw new FileNotFoundException("Repo virtualenv is missing: " + python);
            if (!File.Exists(script)) throw new FileNotFoundException("Proxy script is missing: " + script);
            Directory.CreateDirectory(logDirectory);
            string logPath = Path.Combine(logDirectory, "service-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + "-" + Guid.NewGuid().ToString("N").Substring(0, 6) + ".log");
            // Native inheritable file handles, rather than parent-owned pipe readers, keep logging
            // alive after the manager exits and leaves the service running.
            HiddenLauncher.Launch(python, "-u " + Paths.Quote(script), root, port, logPath);
        }
    }
}
