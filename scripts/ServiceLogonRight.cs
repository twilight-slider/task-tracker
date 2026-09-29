using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class TaskFolderServiceLogonRight
{
    private const uint PolicyCreateAccount = 0x00000010;
    private const uint PolicyLookupNames = 0x00000800;
    private const string Right = "SeServiceLogonRight";

    [StructLayout(LayoutKind.Sequential)]
    private struct LsaObjectAttributes
    {
        public uint Length;
        public IntPtr RootDirectory;
        public IntPtr ObjectName;
        public uint Attributes;
        public IntPtr SecurityDescriptor;
        public IntPtr SecurityQualityOfService;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct LsaUnicodeString
    {
        public ushort Length;
        public ushort MaximumLength;
        public IntPtr Buffer;
    }

    [DllImport("advapi32.dll")]
    private static extern uint LsaOpenPolicy(IntPtr systemName, ref LsaObjectAttributes attributes, uint access, out IntPtr policy);

    [DllImport("advapi32.dll")]
    private static extern uint LsaEnumerateAccountRights(IntPtr policy, byte[] sid, out IntPtr rights, out uint count);

    [DllImport("advapi32.dll")]
    private static extern uint LsaAddAccountRights(IntPtr policy, byte[] sid, ref LsaUnicodeString rights, uint count);

    [DllImport("advapi32.dll")]
    private static extern uint LsaNtStatusToWinError(uint status);

    [DllImport("advapi32.dll")]
    private static extern uint LsaFreeMemory(IntPtr buffer);

    [DllImport("advapi32.dll")]
    private static extern uint LsaClose(IntPtr policy);

    private static IntPtr Open(uint access)
    {
        var attributes = new LsaObjectAttributes { Length = (uint)Marshal.SizeOf(typeof(LsaObjectAttributes)) };
        IntPtr policy;
        Check(LsaOpenPolicy(IntPtr.Zero, ref attributes, access, out policy));
        return policy;
    }

    private static void Check(uint status)
    {
        if (status != 0) throw new Win32Exception((int)LsaNtStatusToWinError(status));
    }

    public static bool Has(byte[] sid)
    {
        var policy = Open(PolicyLookupNames);
        try
        {
            IntPtr rights;
            uint count;
            var status = LsaEnumerateAccountRights(policy, sid, out rights, out count);
            if (status != 0)
            {
                if (LsaNtStatusToWinError(status) == 2) return false;
                Check(status);
            }
            try
            {
                var size = Marshal.SizeOf(typeof(LsaUnicodeString));
                for (var i = 0; i < count; i++)
                {
                    var item = (LsaUnicodeString)Marshal.PtrToStructure(IntPtr.Add(rights, i * size), typeof(LsaUnicodeString));
                    if (String.Equals(Marshal.PtrToStringUni(item.Buffer, item.Length / 2), Right, StringComparison.Ordinal)) return true;
                }
                return false;
            }
            finally { LsaFreeMemory(rights); }
        }
        finally { LsaClose(policy); }
    }

    public static bool Grant(byte[] sid)
    {
        if (Has(sid)) return false;
        var policy = Open(PolicyLookupNames | PolicyCreateAccount);
        var buffer = Marshal.StringToHGlobalUni(Right);
        try
        {
            var right = new LsaUnicodeString {
                Length = (ushort)(Right.Length * 2), MaximumLength = (ushort)((Right.Length + 1) * 2), Buffer = buffer
            };
            Check(LsaAddAccountRights(policy, sid, ref right, 1));
        }
        finally { Marshal.FreeHGlobal(buffer); LsaClose(policy); }
        if (!Has(sid)) throw new InvalidOperationException("SeServiceLogonRight was not granted.");
        return true;
    }
}
