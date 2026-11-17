<?php

// Run by apply-upgrade-1.9.sh with Rector 2.1.0, before rector-normalise.php.
// Reproduces 1.9.0's (string) casts on untyped string arguments; Rector 2.1.7+
// only rewrites literal nulls.

declare(strict_types=1);

use Rector\Config\RectorConfig;
use Rector\Php81\Rector\FuncCall\NullToStrictStringFuncCallArgRector;
use Rector\ValueObject\PhpVersion;

return RectorConfig::configure()
    ->withPhpVersion(PhpVersion::PHP_82)
    ->withPaths([
        __DIR__.'/functions',
        __DIR__.'/public',
    ])
    ->withSkip([
        'functions/PEAR',
        'functions/adLDAP',
        'functions/php-excel-reader',
    ])
    ->withRules([
        NullToStrictStringFuncCallArgRector::class,
    ]);
