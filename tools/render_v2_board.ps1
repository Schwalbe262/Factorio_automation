param([string]$Runtime = 'runtime/v2/dev-101', [string]$Output = 'docs/v2/images/pcb-layout-current.png')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$observation = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $Runtime 'layout-observation.json') | ConvertFrom-Json
$board = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $Runtime 'pcb-board.json') | ConvertFrom-Json
$mining = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $Runtime 'mining-outposts.json') | ConvertFrom-Json
$builder = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $Runtime 'factory-builder.json') | ConvertFrom-Json
$plans = @($builder.coal_plan, $builder.power_plan)
$plans += @($board.blocks.PSObject.Properties | ForEach-Object { $_.Value })
$plans += @($mining.banks | ForEach-Object { $_.plan })
$actual = @{}
foreach ($e in $observation.entities) { $actual["$($e.name):$($e.position.x):$($e.position.y)"] = $true }
$entities = @{}
foreach ($e in @($observation.entities) + @($plans | ForEach-Object { $_.entities })) {
    $entities["$($e.name):$($e.position.x):$($e.position.y)"] = $e
}
$scale = 6
$blocks = @($board.blocks.PSObject.Properties | ForEach-Object { $_.Value })
$minX = [math]::Floor(([math]::Min(-60, ($blocks | ForEach-Object { $_.reserved_area[0][0] } | Measure-Object -Minimum).Minimum)-8)/8)*8
$maxX = [math]::Ceiling(([math]::Max(64, ($blocks | ForEach-Object { $_.reserved_area[1][0] } | Measure-Object -Maximum).Maximum)+8)/8)*8
$minY = [math]::Floor(([math]::Min(-72, ($blocks | ForEach-Object { $_.reserved_area[0][1] } | Measure-Object -Minimum).Minimum)-8)/8)*8
$maxY = [math]::Ceiling(([math]::Max(32, ($blocks | ForEach-Object { $_.reserved_area[1][1] } | Measure-Object -Maximum).Maximum)+8)/8)*8
$plotWidth = ($maxX-$minX)*$scale
$plotHeight = ($maxY-$minY)*$scale
$bitmap = [System.Drawing.Bitmap]::new([int]($plotWidth+72), [int]($plotHeight+180))
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.Clear([System.Drawing.ColorTranslator]::FromHtml('#f8fafc'))
$font = New-Object System.Drawing.Font('Segoe UI', 13)
$small = New-Object System.Drawing.Font('Segoe UI', 9)
$grid = New-Object System.Drawing.Pen([System.Drawing.ColorTranslator]::FromHtml('#e2e8f0'), 1)
$outline = New-Object System.Drawing.Pen([System.Drawing.ColorTranslator]::FromHtml('#94a3b8'), 1)
$zone = New-Object System.Drawing.Pen([System.Drawing.ColorTranslator]::FromHtml('#1d4ed8'), 2)
$arrow = New-Object System.Drawing.Pen([System.Drawing.Color]::White, 1)
$zone.DashStyle = [System.Drawing.Drawing2D.DashStyle]::Dash
function Px([double]$x) { return [single](36 + ($x - $minX) * $scale) }
function Py([double]$y) { return [single](90 + ($y - $minY) * $scale) }
try {
    $graphics.DrawString('PCB factory: reserved blocks, fixed ports, orthogonal corridors', $font, [System.Drawing.Brushes]::Black, 30, 18)
    $graphics.DrawString("Live entities at tick $($observation.tick). Filled = placed; outline = planned. Grid: 8 tiles.", $small, [System.Drawing.Brushes]::DimGray, 30, 48)
    for ($x = $minX; $x -le $maxX; $x += 8) { $graphics.DrawLine($grid, (Px $x), (Py $minY), (Px $x), (Py $maxY)) }
    for ($y = $minY; $y -le $maxY; $y += 8) { $graphics.DrawLine($grid, (Px $minX), (Py $y), (Px $maxX), (Py $y)) }
    $graphics.SetClip((New-Object System.Drawing.RectangleF((Px $minX), (Py $minY), $plotWidth, $plotHeight)))
    foreach ($property in $board.blocks.PSObject.Properties) {
        $a = $property.Value.reserved_area
        $graphics.DrawRectangle($zone, (Px $a[0][0]), (Py $a[0][1]), [single](($a[1][0]-$a[0][0])*$scale), [single](($a[1][1]-$a[0][1])*$scale))
        $graphics.DrawString(($property.Name -replace '_',' '), $small, [System.Drawing.Brushes]::DarkBlue, (Px $a[0][0]), [single]((Py $a[0][1])-18))
    }
    foreach ($e in $entities.Values) {
        $w = 1; $h = 1; $color = '#64748b'
        switch -Regex ($e.name) {
            '^transport-belt$' { $color = '#64748b' }
            '^underground-belt$' { $color = '#0284c7' }
            '^splitter$' { $w = 2; $color = '#7c3aed' }
            '^.*furnace$' { $w = 2; $h = 2; $color = '#d97706' }
            '^electric-mining-drill$' { $w = 3; $h = 3; $color = '#0f766e' }
            '^burner-mining-drill$' { $w = 2; $h = 2; $color = '#a8a29e' }
            '^assembling-machine' { $w = 3; $h = 3; $color = '#be185d' }
            '^lab$' { $w = 3; $h = 3; $color = '#9333ea' }
            '^gun-turret$' { $w = 2; $h = 2; $color = '#991b1b' }
            '^small-electric-pole$' { $w = .4; $h = .4; $color = '#ca8a04' }
            '^.*inserter$' { $w = .45; $h = .45; $color = '#eab308' }
            '^steam-engine$' { $w = 3; $h = 5; $color = '#2563eb' }
            '^boiler$' { $w = 3; $h = 2; $color = '#2563eb' }
        }
        if ($e.direction -in @(4,12)) { $temp = $w; $w = $h; $h = $temp }
        $left = (Px $e.position.x)-$w*$scale/2+.4; $top = (Py $e.position.y)-$h*$scale/2+.4
        if ($actual.ContainsKey("$($e.name):$($e.position.x):$($e.position.y)")) {
            $brush = New-Object System.Drawing.SolidBrush([System.Drawing.ColorTranslator]::FromHtml($color))
            try { $graphics.FillRectangle($brush, [single]$left, [single]$top, [single]($w*$scale-.8), [single]($h*$scale-.8)) } finally { $brush.Dispose() }
        } else { $graphics.DrawRectangle($outline, [single]$left, [single]$top, [single]($w*$scale-.8), [single]($h*$scale-.8)) }
        if ($e.name -in @('transport-belt','underground-belt')) {
            $dx = 0; $dy = 0
            switch ($e.direction) { 0 { $dy = -2 }; 4 { $dx = 2 }; 8 { $dy = 2 }; 12 { $dx = -2 } }
            $graphics.DrawLine($arrow, (Px $e.position.x), (Py $e.position.y), [single]((Px $e.position.x)+$dx), [single]((Py $e.position.y)+$dy))
        }
    }
    foreach ($bank in $mining.banks) {
        $p = $bank.plan.ports[0].position
        $graphics.DrawString($bank.plan.resource, $small, [System.Drawing.Brushes]::DarkGreen, [single]((Px $p.x)+7), [single]((Py $p.y)-18))
    }
    $graphics.ResetClip()
    $graphics.DrawString('Teal: electric mine | orange: furnace | pink: assembler | blue: underground | purple: splitter', $small, [System.Drawing.Brushes]::DimGray, 30, ($plotHeight+120))
    $graphics.DrawString('Geometric engineering diagram. Native item output and sustained throughput require separate game evidence.', $small, [System.Drawing.Brushes]::DimGray, 30, ($plotHeight+142))
    $bitmap.Save([System.IO.Path]::GetFullPath($Output), [System.Drawing.Imaging.ImageFormat]::Png)
    Write-Output $Output
} finally {
    $graphics.Dispose(); $bitmap.Dispose(); $font.Dispose(); $small.Dispose()
    $grid.Dispose(); $outline.Dispose(); $zone.Dispose(); $arrow.Dispose()
}
