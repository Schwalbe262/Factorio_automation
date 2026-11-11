param([string]$Runtime = 'runtime/v2/dev-101')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$before = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $Runtime 'layout-before.json') | ConvertFrom-Json
$migration = Get-Content -Encoding UTF8 -LiteralPath (Join-Path $Runtime 'power-layout-migration.json') | ConvertFrom-Json
$panels = @($before.coal_plan, $migration.new_plan)
$scale = 8
$bitmap = New-Object System.Drawing.Bitmap(1100, 660)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.Clear([System.Drawing.Color]::FromArgb(248,250,252))
$font = New-Object System.Drawing.Font('Segoe UI', 12)
$small = New-Object System.Drawing.Font('Segoe UI', 8)
$gridPen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(224,231,239), 1)
$arrowPen = New-Object System.Drawing.Pen([System.Drawing.Color]::White, 1)
$beltBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(43,104,173))
$machineBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(35,50,65))
$powerBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(205,139,37))
try {
    for ($panel = 0; $panel -lt 2; $panel++) {
        $top = 50 + 315 * $panel
        $title = if ($panel -eq 0) { 'Previous plan: 140 belts, 13-tile self-feed, 6 trunk turns' } else { 'Rebuilt plan: 132 belts, 4-tile self-feed, 4 trunk turns' }
        $graphics.DrawString($title, $font, [System.Drawing.Brushes]::Black, 30, $top - 32)
        for ($laneX = -56; $laneX -le 64; $laneX += 8) {
            $px = 35 + ($laneX + 56) * $scale
            $graphics.DrawLine($gridPen, $px, $top, $px, $top + 240)
        }
        for ($laneY = -8; $laneY -le 24; $laneY += 8) {
            $py = $top + ($laneY + 8) * $scale
            $graphics.DrawLine($gridPen, 35, $py, 1040, $py)
        }
        foreach ($entity in @($panels[$panel].entities) + @($before.power_plan.entities)) {
            $x = 35 + ($entity.position.x + 56) * $scale
            $y = $top + ($entity.position.y + 8) * $scale
            if ($entity.name -eq 'transport-belt') {
                $graphics.FillRectangle($beltBrush, [single]($x-3.5), [single]($y-3.5), [single]7, [single]7)
                $dx = 0; $dy = 0
                switch ($entity.direction) { 0 { $dy = -2 }; 4 { $dx = 2 }; 8 { $dy = 2 }; 12 { $dx = -2 } }
                $graphics.DrawLine($arrowPen, [single]$x, [single]$y, [single]($x+$dx), [single]($y+$dy))
            } elseif ($entity.name -eq 'burner-mining-drill') {
                $graphics.FillRectangle($machineBrush, [single]($x-8), [single]($y-8), [single]16, [single]16)
                $graphics.DrawString('Coal drill', $small, [System.Drawing.Brushes]::Black, [single]($x-20), [single]($y+25))
            } elseif ($entity.name -in @('boiler','steam-engine','offshore-pump')) {
                $graphics.FillRectangle($powerBrush, [single]($x-6), [single]($y-6), [single]12, [single]12)
            }
        }
        $graphics.DrawString('Fixed 8-tile trunk grid. Diagram shows coal/power plans; not a rendered game screenshot.', $small, [System.Drawing.Brushes]::DimGray, 35, $top+253)
    }
    $destination = Join-Path $Runtime 'coal-layout-comparison.png'
    $bitmap.Save([System.IO.Path]::GetFullPath($destination), [System.Drawing.Imaging.ImageFormat]::Png)
    Write-Output $destination
} finally {
    $graphics.Dispose(); $bitmap.Dispose(); $font.Dispose(); $small.Dispose()
    $gridPen.Dispose(); $arrowPen.Dispose(); $beltBrush.Dispose(); $machineBrush.Dispose(); $powerBrush.Dispose()
}
