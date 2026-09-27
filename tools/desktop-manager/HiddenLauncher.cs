using System;
using System.Collections;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace BpsManager
{
    internal static class HiddenLauncher
    {
        [StructLayout(LayoutKind.Sequential)] private struct SECURITY_ATTRIBUTES { public int Length; public IntPtr Descriptor; [MarshalAs(UnmanagedType.Bool)] public bool Inherit; }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] private struct STARTUPINFO
        {
            public int cb; public string reserved, desktop, title; public int x, y, width, height, xChars, yChars, fill, flags;
            public short show, reservedCount; public IntPtr reserved2, input, output, error;
        }
        [StructLayout(LayoutKind.Sequential)] private struct STARTUPINFOEX { public STARTUPINFO Info; public IntPtr Attributes; }
        [StructLayout(LayoutKind.Sequential)] private struct PROCESS_INFORMATION { public IntPtr Process, Thread; public int Pid, Tid; }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern IntPtr CreateFile(string name, uint access, uint share, ref SECURITY_ATTRIBUTES security, uint disposition, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError = true)] private static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll", SetLastError = true)] private static extern bool InitializeProcThreadAttributeList(IntPtr list, int count, int flags, ref IntPtr size);
        [DllImport("kernel32.dll", SetLastError = true)] private static extern bool UpdateProcThreadAttribute(IntPtr list, uint flags, IntPtr attribute, IntPtr value, IntPtr size, IntPtr previous, IntPtr returned);
        [DllImport("kernel32.dll")] private static extern void DeleteProcThreadAttributeList(IntPtr list);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern bool CreateProcess(string app, StringBuilder command, IntPtr processSecurity, IntPtr threadSecurity, bool inherit, uint flags, IntPtr environment, string directory, ref STARTUPINFOEX startup, out PROCESS_INFORMATION process);
        internal static Dictionary<string, string> ChildEnvironment(int port)
        {
            Settings.ValidatePort(port);
            Dictionary<string, string> values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (DictionaryEntry entry in Environment.GetEnvironmentVariables()) values[(string)entry.Key] = (string)entry.Value;
            values["GHCP_PORT"] = port.ToString(System.Globalization.CultureInfo.InvariantCulture);
            return values;
        }
        public static int Launch(string exe, string arguments, string root, int port, string logPath)
        {
            SECURITY_ATTRIBUTES security = new SECURITY_ATTRIBUTES { Length = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES)), Inherit = true };
            IntPtr log = new IntPtr(-1), input = new IntPtr(-1), list = IntPtr.Zero, handles = IntPtr.Zero, env = IntPtr.Zero;
            bool initialized = false;
            try
            {
                log = CreateFile(logPath, 0x40000000, 7, ref security, 1, 0x80, IntPtr.Zero);
                if (log == new IntPtr(-1)) throw new Win32Exception();
                input = CreateFile("NUL", 0x80000000, 3, ref security, 3, 0x80, IntPtr.Zero);
                if (input == new IntPtr(-1)) throw new Win32Exception();
                IntPtr bytes = IntPtr.Zero; InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref bytes);
                list = Marshal.AllocHGlobal(bytes);
                if (!InitializeProcThreadAttributeList(list, 1, 0, ref bytes)) throw new Win32Exception();
                initialized = true; handles = Marshal.AllocHGlobal(IntPtr.Size * 2);
                Marshal.WriteIntPtr(handles, 0, input); Marshal.WriteIntPtr(handles, IntPtr.Size, log);
                if (!UpdateProcThreadAttribute(list, 0, new IntPtr(0x20002), handles, new IntPtr(IntPtr.Size * 2), IntPtr.Zero, IntPtr.Zero)) throw new Win32Exception();
                SortedDictionary<string, string> environment = new SortedDictionary<string, string>(ChildEnvironment(port), StringComparer.OrdinalIgnoreCase);
                StringBuilder block = new StringBuilder();
                foreach (KeyValuePair<string, string> entry in environment) block.Append(entry.Key).Append('=').Append(entry.Value).Append('\0');
                block.Append('\0'); env = Marshal.StringToHGlobalUni(block.ToString());
                STARTUPINFOEX startup = new STARTUPINFOEX();
                startup.Info.cb = Marshal.SizeOf(typeof(STARTUPINFOEX)); startup.Attributes = list;
                startup.Info.flags = 0x101; startup.Info.show = 0; startup.Info.input = input; startup.Info.output = log; startup.Info.error = log;
                PROCESS_INFORMATION process;
                if (!CreateProcess(exe, new StringBuilder(Paths.Quote(exe) + " " + arguments), IntPtr.Zero, IntPtr.Zero, true,
                    0x08000000 | 0x00000400 | 0x00080000, env, root, ref startup, out process)) throw new Win32Exception();
                try { return process.Pid; } finally { CloseHandle(process.Thread); CloseHandle(process.Process); }
            }
            finally
            {
                if (initialized) DeleteProcThreadAttributeList(list);
                if (list != IntPtr.Zero) Marshal.FreeHGlobal(list); if (handles != IntPtr.Zero) Marshal.FreeHGlobal(handles);
                if (env != IntPtr.Zero) Marshal.FreeHGlobal(env);
                if (input != new IntPtr(-1)) CloseHandle(input); if (log != new IntPtr(-1)) CloseHandle(log);
            }
        }
    }
}
