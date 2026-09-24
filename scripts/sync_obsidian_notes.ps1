#Requires -Version 5.1
<#
.SYNOPSIS
  公開してよい Obsidian .md だけをリポジトリの obsidian/ へコピーする（方法1）。

.DESCRIPTION
  vault 実体はユーザー PC 上。このスクリプトは指定したソースから .md のみをコピーする。
  秘密・顧客名・認証情報を含むノートは渡さないこと。

.EXAMPLE
  # vault 内「公開用」フォルダから
  .\scripts\sync_obsidian_notes.ps1 -Source "C:\Users\info\Obsidian Vault\公開用"

.EXAMPLE
  # 個別ファイル
  .\scripts\sync_obsidian_notes.ps1 -Files @(
    "C:\Users\info\Obsidian Vault\メモ\方針.md"
  )
#>
param(
    [string] $Source = "",
    [string[]] $Files = @(),
    [string] $RepoRoot = "",
    [switch] $WhatIf
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$Dest = Join-Path $RepoRoot "obsidian"
if (-not (Test-Path -LiteralPath $Dest -PathType Container)) {
    throw "obsidian/ が見つかりません: $Dest"
}

$copied = @()

if ($Source) {
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Source フォルダがありません: $Source"
    }
    Get-ChildItem -LiteralPath $Source -Filter "*.md" -File | ForEach-Object {
        $target = Join-Path $Dest $_.Name
        if ($WhatIf) {
            Write-Host "WhatIf: $($_.FullName) -> $target"
        } else {
            Copy-Item -LiteralPath $_.FullName -Destination $target -Force
        }
        $copied += $_.Name
    }
}

foreach ($f in $Files) {
    if (-not (Test-Path -LiteralPath $f -PathType Leaf)) {
        throw "ファイルがありません: $f"
    }
    if ([IO.Path]::GetExtension($f) -ne ".md") {
        throw ".md 以外はコピーしません: $f"
    }
    $name = [IO.Path]::GetFileName($f)
    $target = Join-Path $Dest $name
    if ($WhatIf) {
        Write-Host "WhatIf: $f -> $target"
    } else {
        Copy-Item -LiteralPath $f -Destination $target -Force
    }
    $copied += $name
}

if ($copied.Count -eq 0) {
    Write-Warning "コピー対象がありません。-Source または -Files を指定してください。"
    exit 1
}

Write-Host "コピー完了 ($($copied.Count) 件):"
$copied | Sort-Object -Unique | ForEach-Object { Write-Host "  - $_" }
Write-Host ""
Write-Host "次: git add obsidian/ && git status で秘密が混ざっていないか確認してから commit / push"
