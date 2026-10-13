# Create desktop shortcut for LectureLens (one-off helper)
$ErrorActionPreference = "Stop"

# Chinese name via code points, keeps this file pure ASCII (PS5.1 BOM pitfall)
$name = 'LectureLens ' + [string]::Join('', [char[]](35838,22530,23383,24149))
$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop ($name + '.lnk')

$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = 'D:\projects\lecture-translate\start_translate.bat'
$lnk.WorkingDirectory = 'D:\projects\lecture-translate'
$lnk.IconLocation = 'D:\projects\lecture-translate\.venv\Scripts\python.exe,0'
$lnk.Description = 'LectureLens lecture live-caption translation'
$lnk.Save()

if (Test-Path -LiteralPath $lnkPath) {
    Write-Output ("OK  " + $lnkPath)
} else {
    Write-Output "FAILED"
    exit 1
}
