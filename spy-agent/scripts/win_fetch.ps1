# Fetch a URL using your Windows login (Kerberos/NTLM) and/or smart-card certificate,
# for internal sites Python's stdlib cannot authenticate to.
# Used by Spy via config:  "http_command": "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\path\\to\\spy\\scripts\\win_fetch.ps1 -Url {url} -Out {out}"
param(
    [Parameter(Mandatory=$true)][string]$Url,
    [Parameter(Mandatory=$true)][string]$Out,
    [string]$CertThumbprint = ""   # optional: PKI cert from Cert:\CurrentUser\My
)
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$params = @{ Uri = $Url; OutFile = $Out; UseDefaultCredentials = $true; UseBasicParsing = $true; TimeoutSec = 60 }
if ($CertThumbprint) { $params.Certificate = Get-Item "Cert:\CurrentUser\My\$CertThumbprint" }
Invoke-WebRequest @params
