' ===========================================================================
'  WheresMyGPU — 后台静默启动（不显示任何窗口）
'  双击本文件即可。日志写入 logs\ 目录。
'  停止：运行 stop_monitor.bat，或在任务管理器里结束 python.exe
' ===========================================================================
Option Explicit

Dim fso, shell, baseDir, pyExe, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)

' 选择 Python 解释器：优先本机 Anaconda，其次 PATH 里的 python
pyExe = "D:\anaconda_root\python.exe"
If Not fso.FileExists(pyExe) Then
    pyExe = "python"
End If

cmd = """" & pyExe & """ -m wheresmygpu"

' 0 = 隐藏窗口, False = 不等待
shell.CurrentDirectory = baseDir
shell.Run cmd, 0, False

WScript.Echo "WheresMyGPU 已在后台启动。" & vbCrLf & _
             "  配置文件: " & baseDir & "\config.yaml" & vbCrLf & _
             "  日志目录: " & baseDir & "\logs" & vbCrLf & vbCrLf & _
             "如需停止，请运行 stop_monitor.bat"