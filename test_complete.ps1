

# SIMPLE TEST SCRIPT - Using image from desktop

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "TESTING PRODUCTS & SCAN SYSTEM" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# Step 1: Login users
Write-Host "`n[1] LOGGING IN USERS..." -ForegroundColor Yellow

# Login Alice
$body = '{"email":"alice@example.com","password":"alice123"}'
$response = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/users/auth/login/" -Method POST -ContentType "application/json" -Body $body
$TOKEN_A = ($response.Content | ConvertFrom-Json).token
Write-Host "Alice logged in" -ForegroundColor Green

# Login Bob
$response = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/users/auth/login/" -Method POST -ContentType "application/json" -Body $body
$TOKEN_B = ($response.Content | ConvertFrom-Json).token
Write-Host "Bob logged in" -ForegroundColor Green

# Step 2: Use an image from your desktop
Write-Host "`n[2] USING IMAGE FROM DESKTOP..." -ForegroundColor Yellow

# Change this path to your actual image path
$IMAGE_PATH = "E:\9raya_4eme_Sem2\Pi\ProductIntelligence\Image_Test\Mixa_QR.png"

if (-not (Test-Path $IMAGE_PATH)) {
    Write-Host "Image not found at: $IMAGE_PATH" -ForegroundColor Red
    Write-Host "Please update the path to your image" -ForegroundColor Yellow
    exit
}

Write-Host "Using image: $IMAGE_PATH" -ForegroundColor Green

# Step 3: Create scan using .NET WebClient (more reliable)
Write-Host "`n[3] ALICE SCANNING PRODUCT..." -ForegroundColor Yellow

Add-Type -AssemblyName System.Net.Http

$boundary = "---------------------------" + [System.Guid]::NewGuid().ToString()
$lineBreak = "rn"

$fileBytes = [System.IO.File]::ReadAllBytes($IMAGE_PATH)

$sb = New-Object System.Text.StringBuilder
$sb.Append("--$boundary$lineBreak") | Out-Null
$sb.Append("Content-Disposition: form-data; name=`"image`"; filename=`"product.jpg`"$lineBreak") | Out-Null
$sb.Append("Content-Type: image/jpeg$lineBreak$lineBreak") | Out-Null

$preBytes = [System.Text.Encoding]::UTF8.GetBytes($sb.ToString())
$postBytes = [System.Text.Encoding]::UTF8.GetBytes("$lineBreak--$boundary--$lineBreak")

$fullBytes = $preBytes + $fileBytes + $postBytes

$headers = @{
    "Authorization" = "Token $TOKEN_A"
    "Content-Type" = "multipart/form-data; boundary=$boundary"
}

try {
    $result = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/scan/" -Method POST -Headers $headers -Body $fullBytes -ErrorAction Stop
    $scanResult = $result.Content | ConvertFrom-Json
    $PRODUCT_ID = $scanResult.product_id
    Write-Host "Product created with ID: $PRODUCT_ID" -ForegroundColor Green
    Write-Host "User decision: $($scanResult.user_decision)" -ForegroundColor Cyan
} catch {
    Write-Host "Scan failed: $_" -ForegroundColor Red
    Write-Host "`nTrying alternative method..." -ForegroundColor Yellow
    
    # Alternative: Use curl command
    curl.exe -X POST http://127.0.0.1:8000/api/scan/ -H "Authorization: Token $TOKEN_A" -F "image=@$IMAGE_PATH"
    exit
}

# Continue with the rest of the tests...
Write-Host "`n[4] CHECKING PENDING PRODUCTS..." -ForegroundColor Yellow
$response = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/products/my-products/?status=pending" -Headers @{"Authorization"="Token $TOKEN_A"}
$pending = $response.Content | ConvertFrom-Json
Write-Host "Pending products: $($pending.products.Count)" -ForegroundColor Green

Write-Host "`n[5] ALICE APPROVING PRODUCT..." -ForegroundColor Yellow
$approveBody = '{"decision":"approved","notes":"Works great"}'
$response = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/products/$PRODUCT_ID/decision/" -Method PATCH -Headers @{
    "Authorization" = "Token $TOKEN_A"
    "Content-Type" = "application/json"
} -Body $approveBody
Write-Host "Product approved!" -ForegroundColor Green

Write-Host "`n[6] CHECKING ACTIVE PRODUCTS..." -ForegroundColor Yellow
$response = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/products/my-products/" -Headers @{"Authorization"="Token $TOKEN_A"}
$aliceProducts = $response.Content | ConvertFrom-Json
Write-Host "Alice's products: $($aliceProducts.products.Count)" -ForegroundColor Green

if ($aliceProducts.products.Count -gt 0) {
    Write-Host "  Product: $($aliceProducts.products[0].name)" -ForegroundColor Cyan
    Write-Host "  Decision: $($aliceProducts.products[0].user_decision)" -ForegroundColor Cyan
}

Write-Host "`n[7] BOB SCANNING SAME PRODUCT..." -ForegroundColor Yellow

$result = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/scan/" -Method POST -Headers $headers -Body $fullBytes -ErrorAction Stop
$scanResultB = $result.Content | ConvertFrom-Json
Write-Host "Bob scanned - Product ID: $($scanResultB.product_id)" -ForegroundColor Green

Write-Host "`n[8] BOB REJECTING PRODUCT..." -ForegroundColor Yellow
$rejectBody = '{"decision":"rejected","notes":"Causes issues"}'
$response = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/products/$PRODUCT_ID/decision/" -Method PATCH -Headers @{
    "Authorization" = "Token $TOKEN_B"
    "Content-Type" = "application/json"
} -Body $rejectBody
Write-Host "Product rejected!" -ForegroundColor Green

Write-Host "`n[9] CHECKING BOB'S REJECTED PRODUCTS..." -ForegroundColor Yellow
$response = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/products/my-products/?status=rejected" -Headers @{"Authorization"="Token $TOKEN_B"}
$bobRejected = $response.Content | ConvertFrom-Json
Write-Host "Bob's rejected products: $($bobRejected.products.Count)" -ForegroundColor Green

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "TEST COMPLETE!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan