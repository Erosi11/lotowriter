# Dev-only helper (Windows): convert DOCX to PDF with Microsoft Word.
# Production on Linux uses LibreOffice headless instead.
param([Parameter(Mandatory=$true)][string]$In, [Parameter(Mandatory=$true)][string]$Out)
$word = New-Object -ComObject Word.Application
$word.Visible = $false
try {
    $doc = $word.Documents.Open((Resolve-Path $In).Path, $false, $true)
    $doc.SaveAs([ref][System.IO.Path]::GetFullPath($Out), [ref]17)
    $doc.Close($false)
} finally { $word.Quit() }
