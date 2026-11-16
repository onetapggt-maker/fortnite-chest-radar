$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
    $_.GetGenericArguments().Length -eq 1 -and $_.GetParameters().Length -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1

function Read-Operation($operation, [Type] $resultType) {
    $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
    $task.GetAwaiter().GetResult()
}

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage(
    [Windows.Globalization.Language]::new('ru'))
if ($null -eq $engine) {
    [Console]::WriteLine('no-russian-ocr')
    exit 1
}
[Console]::WriteLine('ready')

while ($null -ne ($line = [Console]::ReadLine())) {
    $memory = $stream = $bitmap = $null
    try {
        $memory = [IO.MemoryStream]::new([Convert]::FromBase64String($line))
        $stream = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($memory)
        $decoder = Read-Operation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Read-Operation ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Read-Operation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $lines = @(
            foreach ($ocrLine in $result.Lines) {
                $boxes = @($ocrLine.Words | ForEach-Object { $_.BoundingRect })
                if ($boxes.Count -eq 0) { continue }
                $left = ($boxes | Measure-Object -Property X -Minimum).Minimum
                $top = ($boxes | Measure-Object -Property Y -Minimum).Minimum
                $right = ($boxes | ForEach-Object { $_.X + $_.Width } | Measure-Object -Maximum).Maximum
                $bottom = ($boxes | ForEach-Object { $_.Y + $_.Height } | Measure-Object -Maximum).Maximum
                @{ text = $ocrLine.Text; box = @($left, $top, $right, $bottom) }
            }
        )
        [Console]::WriteLine((ConvertTo-Json -InputObject $lines -Compress))
    } catch {
        [Console]::WriteLine('ocr-error')
    } finally {
        if ($bitmap) { $bitmap.Dispose() }
        if ($stream) { $stream.Dispose() }
        if ($memory) { $memory.Dispose() }
    }
}
