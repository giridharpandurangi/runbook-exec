# Test runbook with failing step

## Diagnose

1. This step succeeds.

   ```cmd
   echo Step 1 ran successfully
   ```

2. This step will fail (PowerShell in cmd shell).

   ```cmd
   Get-PSDrive
   ```

3. This step would run if you continue.

   ```cmd
   echo Step 3 ran
   ```
