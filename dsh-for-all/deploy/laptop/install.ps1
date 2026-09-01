# install.ps1 —— dsh-for-all 个人电脑（Windows 笔记本/桌面）场景一键部署
#
# 个人电脑场景目标（继承 dsh-laptop/ 专版经验）：开机不自启、锚定锚进程（默认 DSH Desktop）
# 运行才抢任务、资源占用低、退出干净不留残余进程。
#
# 机制：
#   计划任务 TaskHubGuard（每 60 秒）→ pythonw.exe guard_all.py --once
#   （pythonw 为 GUI 子系统，全程无控制台窗口；无需管理员）
#   guard_all.py 以 TASKHUB_MODE=laptop 巡检：
#     锚进程在运行 → 确保恰好一个 worker_all.py 存活（命令行检测 + pid 兜底 + 去重）
#     锚进程不在   → 结束 worker（taskkill /T 连执行会话一起收干净）
#
# 用法（仓库克隆根目录，PowerShell）:
#   powershell -ExecutionPolicy Bypass -File dsh-for-all\deploy\laptop\install.ps1 `
#       -ProjectRoot E:\DSH -Token github_pat_xxx [-WorkerName dsh-laptop] `
#       [-PythonExe C:\path\python.exe] [-PythonwExe C:\path\pythonw.exe] `
#       [-AnchorProcess "DSH Desktop"] [-TaskName TaskHubGuard] [-Proxy http://127.0.0.1:10891] `
#       [-DisableLegacyGuard]
#
#   -DisableLegacyGuard：同时卸载 dsh-laptop 旧守卫计划任务（TaskHubWorkerGuard），
#   避免 worker_laptop.py 与 worker_all.py 双守护同名抢任务。

param(
    [Parameter(Mandatory = $true)][string]$ProjectRoot,
    [Parameter(Mandatory = $true)][string]$Token,
    [string]$PythonExe = 'python.exe',
    [string]$PythonwExe = 'pythonw.exe',
    [string]$WorkerName = 'dsh-laptop',
    [string]$AnchorProcess = 'DSH Desktop',
    [string]$TaskName = 'TaskHubGuard',
    [string]$Proxy = 'http://127.0.0.1:10891',
    [switch]$DisableLegacyGuard
)

$ErrorActionPreference = 'Stop'
$EditionDir = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))  # dsh-for-all\（本脚本在 deploy\laptop\ 下，需上溯三级）
$RepoRoot   = Split-Path -Parent $EditionDir                                        # 仓库根
$SkillDir   = Join-Path $ProjectRoot '.dsh\skills\taskhub'
$TaskDir    = Join-Path $ProjectRoot 'taskhub'

# 1) 文件落位（board.py 复用仓库 dsh-laptop/（或 dsh/）里的同一份 v2 客户端，避免拷贝漂移）
New-Item -ItemType Directory -Force -Path $SkillDir, (Join-Path $TaskDir 'inbox') | Out-Null
foreach ($f in 'worker_all.py', 'guard_all.py', 'qq_notify.py', 'notify_check.py') {
    Copy-Item (Join-Path $EditionDir $f) -Destination $SkillDir -Force
}
$boardSrc = Join-Path $RepoRoot 'dsh-laptop\board.py'
if (-not (Test-Path $boardSrc)) { $boardSrc = Join-Path $RepoRoot 'dsh\board.py' }
if (Test-Path $boardSrc) {
    Copy-Item $boardSrc -Destination (Join-Path $SkillDir 'board.py') -Force
} else {
    throw "找不到 board.py（期望 $RepoRoot\dsh-laptop\board.py 或 dsh\board.py）"
}

# 2) 凭据（保留已有）
$creds = Join-Path $TaskDir 'credentials.env'
if (-not (Test-Path $creds)) {
    Set-Content -Path $creds -Value "GITHUB_TOKEN=$Token" -Encoding ascii
    Write-Host "[install] credentials.env written -> $creds"
} else {
    Write-Host "[install] credentials.env kept"
}

# 3) 用户级环境（计划任务与手动运行共用；guard_all.py 与 worker_all.py 都读这套）
$u = {
    param($k, $v) [Environment]::SetEnvironmentVariable($k, $v, 'User')
}
& $u 'TASKHUB_MODE'          'laptop'
& $u 'TASKHUB_ANCHOR'        $AnchorProcess
& $u 'TASKHUB_PYTHON'        $PythonExe
& $u 'TASKHUB_SKILL_DIR'     $SkillDir
& $u 'TASKHUB_TASK_DIR'      $TaskDir
& $u 'TASKHUB_WORKER'        $WorkerName
& $u 'TASKHUB_CREDENTIALS'   $creds
& $u 'TASKHUB_INBOX'         (Join-Path $TaskDir 'inbox\claims.log')
& $u 'TASKHUB_NOTIFY_QQ'     '0'
& $u 'TASKHUB_POLL'          '15'
& $u 'TASKHUB_MAX_LOAD'      '1'
& $u 'TASKHUB_LEASE_MIN'     '30'
& $u 'TASKHUB_HEARTBEAT_MIN' '20'
& $u 'TASKHUB_UNDECLARED'    'skip'
& $u 'HTTPS_PROXY'           $Proxy
& $u 'HTTP_PROXY'            $Proxy
& $u 'PYTHONUTF8'            '1'
& $u 'PYTHONUNBUFFERED'      '1'

# 4) 旧版守卫清理（可选）：防止 worker_laptop.py 与 worker_all.py 双守护同名抢任务
if ($DisableLegacyGuard) {
    foreach ($legacy in 'TaskHubWorkerGuard') {
        $old = Get-ScheduledTask -TaskName $legacy -ErrorAction SilentlyContinue
        if ($old) {
            Unregister-ScheduledTask -TaskName $legacy -Confirm:$false
            Write-Host "[install] 已卸载旧守卫计划任务 $legacy（防双 worker）"
        }
    }
}

# 5) 计划任务：每 60 秒 pythonw 隐藏巡检（无控制台、免管理员、幂等）
#    注意：这只是"每分钟一次秒级检查"，不是开机自启抢任务程序——锚进程不在时什么都不拉起。
$guard = Join-Path $SkillDir 'guard_all.py'
$action    = New-ScheduledTaskAction -Execute $PythonwExe -Argument "-u `"$guard`" --once"
$trigger   = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(5) `
               -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERDOMAIN\$env:USERNAME -LogonType Interactive
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
               -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "[install] done."
Write-Host "  mode    : laptop（锚进程 '$AnchorProcess' 运行才抢任务；DSH 关闭自动停，退出干净）"
Write-Host "  worker  : $WorkerName  skill: $SkillDir  runtime: $TaskDir"
Write-Host "  task    : $TaskName（每 60s, pythonw 隐藏执行, 无管理员, 开机不自启抢任务）"
Write-Host "[install] 自检: Start-ScheduledTask $TaskName; Get-Content $TaskDir\worker.out.log -Tail 5"
