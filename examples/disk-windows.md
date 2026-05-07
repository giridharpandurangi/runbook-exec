# Check Windows disk usage

## Diagnose

1. Show disk usage on all drives.

   ```powershell
   Get-PSDrive -PSProvider FileSystem | Format-Table Name, Used, Free
   ```

2. Find the largest folders on C: drive.

   ```powershell
   Get-ChildItem C:\ -Directory | ForEach-Object { [PSCustomObject]@{ Name = $_.Name; SizeGB = [math]::Round((Get-ChildItem $_.FullName -Recurse -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum / 1GB, 2) } } | Sort-Object SizeGB -Descending | Select-Object -First 10
   ```

3. Find files larger than 100 MB on C: drive.

   ```powershell
   Get-ChildItem C:\ -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.Length -gt 100MB } | Select-Object FullName, @{Name="SizeMB";Expression={[math]::Round($_.Length/1MB, 2)}}
   ```
