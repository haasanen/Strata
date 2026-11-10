import assert from 'node:assert/strict';
import { statSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';

/** The effective protection of a file: mode 0600 on POSIX, the exact DACL (one protected entry for this user) on NTFS. */
export function assertPrivate(path) {
  if (process.platform !== 'win32') { assert.equal(statSync(path).mode & 0o777, 0o600); return; }
  // fork: some Windows runner images cannot load the Microsoft.PowerShell.Security module at all — Get-Acl fails both its
  // implicit autoload and an explicit Import-Module there ("The member Sddl is already present" in the module's type data).
  // icacls + whoami are native binaries with no PowerShell module dependency and show the same DACL.
  const listing = execFileSync(join(process.env.SystemRoot, 'System32', 'icacls.exe'), [path], { encoding: 'utf8', timeout: 30000, windowsHide: true });
  const me = execFileSync(join(process.env.SystemRoot, 'System32', 'whoami.exe'), [], { encoding: 'utf8', timeout: 10000, windowsHide: true }).trim();
  const entries = listing.split(/\r?\n/).filter(line => line.includes(':('));
  assert.equal(entries.length, 1, `DACL is not owner-only: ${listing}`);
  assert.ok(entries[0].toLowerCase().includes(me.toLowerCase()) && entries[0].includes('(F)') && !entries[0].includes('(I)'),
    `DACL entry is not an explicit full-control grant for ${me}: ${entries[0]}`);
}
