# Disk Full Incident Runbook

Use this runbook when a host reports a disk-full alert or when services are failing with "no space left on device" errors. Work through the sections in order: diagnose first, then clean up, then verify. Only proceed to the Emergency section if the Cleanup section does not free enough space.

## Diagnosis

1. Check overall disk usage across all filesystems.

   ```bash
   df -h
   ```

2. Identify the filesystem that is full or nearly full, sorted by usage percentage.

   ```bash
   df -h | sort -k5 -rn | head -20
   ```

3. Find the largest top-level directories consuming space on the affected filesystem.

   ```bash
   du -sh /* 2>/dev/null | sort -rh | head -20
   ```

4. Check for large log files that may have grown unbounded.

   ```bash
   find /var/log -type f -size +100M -exec ls -lh {} \;
   ```

5. Check for deleted files still held open by running processes (these consume space until the process closes them).

   ```bash
   lsof +L1 | grep -v "^COMMAND"
   ```

## Cleanup

6. Force log rotation to free space immediately without deleting files.

   ```bash
   logrotate --force /etc/logrotate.conf
   ```

7. Remove old compressed log archives older than 30 days.

   ```bash
   find /var/log -name "*.gz" -mtime +30 -delete
   ```

8. Clear the systemd journal, keeping only the most recent 100 MB.

   ```bash
   journalctl --vacuum-size=100M
   ```

9. Remove pip and apt package caches to reclaim space.

   ```bash
   pip cache purge && apt-get clean
   ```

## Verification

10. Verify disk usage has improved after cleanup.

    ```bash
    df -h
    ```

11. Check that critical services are still running after the cleanup operations.

    ```bash
    systemctl status nginx postgresql redis 2>&1 | grep -E "Active:|●"
    ```

## Emergency (if still critical)

12. Locate core dump files that may be consuming large amounts of space.

    ```bash
    find / -name "core" -type f -size +10M 2>/dev/null
    ```

13. Remove identified core dump files — verify the paths from step 12 before running this.

    ```bash
    find / -name "core" -type f -size +10M -delete 2>/dev/null
    ```

14. If a runaway process is writing to a log file, truncate the file in place (safer than deleting while the process holds it open).

    ```bash
    truncate -s 0 /var/log/app/runaway.log
    ```
