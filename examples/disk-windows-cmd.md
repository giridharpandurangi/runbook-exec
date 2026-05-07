# Check Windows disk usage (cmd.exe version)

## Diagnose

1. Show free disk space on all drives.

   ```cmd
   wmic logicaldisk get size,freespace,caption
   ```

2. List the largest folders on C: drive.

   ```cmd
   dir C:\ /-c /a:d
   ```

3. Show system info including memory and disk.

   ```cmd
   systeminfo
   ```
