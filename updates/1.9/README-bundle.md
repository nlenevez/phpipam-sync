# phpIPAM 1.8.1 / 1.8.2 → 1.9.0 airgapped upgrade bundle

```sh
sha256sum -c phpipam-upgrade-1.9.0.tgz.sha256
tar xzf phpipam-upgrade-1.9.0.tgz && cd phpipam-upgrade-1.9.0

# back up first
mysqldump <db> > /root/phpipam-pre-1.9.sql

# builds a NEW tree; the live install is only read
sudo ./apply-upgrade-1.9.sh /var/www/phpipam /var/www/phpipam-1.9.0
```

Needs: `git` and `php-cli` ≥ 8.2.

Each file changed against stock 1.8.x is moved to its 1.9.0 path (web files
are under `public/`) and copied, or 3-way merged if upstream changed it. PHP
files are normalised with upstream's Rector + Pint rules first, then merged
ignoring whitespace.

Next steps are printed at the end: database queries (argument **1.8.46**, not
1.8), web server `root …/public`, SELinux, cron paths. Report:
`<new tree>/.upgrade-1.9/report.txt`; your original changes:
`your-changes.diff`.

Exit 0 = clean. Exit 1 = conflicts or PHP syntax errors (listed).
Exit 2 = refused to run (reason printed). The live install is never touched.
