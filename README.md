# Douyin Vietnamese Dubbing

Chrome extension lấy video Douyin đang hiển thị, tạo phụ đề và lồng tiếng Việt trên Google Colab, làm mờ subtitle/watermark gốc rồi tự tải MP4 hoàn chỉnh.

## Cài extension

1. Mở `chrome://extensions` và bật **Developer mode**.
2. Chọn **Load unpacked** rồi chọn thư mục `extension` của dự án.
3. Ghim biểu tượng **Douyin Vietnamese Dubbing** lên thanh công cụ.

## Thiết lập một lần

1. Tạo **Gemini API key mới**. Không dùng lại key đã từng gửi công khai.
2. Mở extension, dán key và bấm **Lưu**. Key chỉ nằm trong Chrome profile hiện tại, không được ghi vào source hoặc đồng bộ.
3. Export cookie Douyin ở định dạng Netscape `cookies.txt`, sau đó chọn file trong extension. Khi cookie hết hạn, extension sẽ yêu cầu import lại.
4. Voice clone là clip một người nói rõ, dài 3–10 giây. Chỉ clone giọng bạn có quyền sử dụng.

## Sử dụng

1. Mở video trên `douyin.com`; có thể là trang feed/discover hoặc URL `/video/<id>`.
2. Bấm biểu tượng extension để mở side panel. Extension tự lấy URL chuẩn của video đang phát.
3. Chọn preset Hoài My/Nam Minh hoặc thêm voice clone. Giữ chế độ **Tự động** và bấm phân tích.
4. Extension tự tạo bản xem trước 30 giây và tự dừng video Douyin gốc để âm thanh không phát chồng. Bấm **Mở preview lớn trong tab riêng** nếu cần xem toàn khung hình; player nhỏ sẽ dừng và tab lớn tiếp tục đúng câu/thời điểm hiện tại. Tab lớn vẫn có thanh tốc độ và nút xuất. Kéo tốc độ giọng trong khoảng 0,80–1,40× để nghe phản hồi tức thì; khi thả thanh, extension tự tạo lại preview chính xác rồi tiếp tục ở vị trí đang nghe. Khi đã khớp giọng gốc, bấm xuất toàn bộ video.
5. Bạn có thể bấm **Run all** trong `OmniVoice_API.ipynb` trước rồi mới bấm **Phân tích video**. Extension sẽ gắn vào đúng tab Colab đang mở và dùng server đã chạy; nó không refresh notebook hoặc mở thêm tab. Nếu chưa có notebook, extension mới tự mở một tab, kết nối GPU T4 và chạy notebook. Giữ tab Colab mở cho tới khi tải bắt đầu.

Pipeline dùng Whisper nhận dạng lời nói rồi để Gemini sửa transcript và viết lại tiếng Việt theo nguyên timestamp từng cue; OCR chỉ phục vụ tìm vùng blur. Nhịp TTS được fit về thời lượng câu gốc trong khoảng tự nhiên 0,90–1,15×. Khi xuất video, một lớp giọng gốc nhỏ được giữ dưới giọng Việt để video vẫn có cảm giác người trong cảnh đang nói.

Với voice clone, OmniVoice được tạo ở 32 bước và nhận trực tiếp thời lượng cue gốc. Sau khi tạo, Whisper kiểm tra phần cuối từng clip giọng và cắt có fade nếu model sinh một cụm từ lặp không có trong câu, chẳng hạn “sao, sao”. Cache TTS cũ sẽ tự bị loại khi thuật toán tạo giọng thay đổi.

Extension kiểm tra đúng phiên bản backend khi nối lại Colab. Nếu notebook vẫn chạy backend cũ, extension tự làm mới notebook và tạo lại job thay vì tiếp tục phát preview/cache cũ.

Chế độ **Chỉnh thủ công** là ngoại lệ có chủ ý: quy trình sẽ dừng sau phân tích để hiện một canvas, cho phép tạo nhiều khung blur màu hồng và đúng một khung subtitle màu xanh; sau đó cần bấm nút tạo video.

### Vì sao lần đầu chạy lâu?

- Extension tái sử dụng đúng một tab Colab, yêu cầu GPU T4 và hiển thị các bước kết nối/cài thư viện/tạo tunnel ngay trong side panel. Nó cũng tự tiêm lại script vào tab Colab/Douyin đã mở sau khi reload extension.
- Lần chạy đầu thường mất vài phút vì Colab phải cài thư viện AI. Khi phân tích hoặc clone giọng lần đầu, Whisper, PaddleOCR, Demucs và OmniVoice còn phải tải model.
- Trong cùng một runtime Colab, dependency được cache theo nội dung `requirements-colab.txt`; chạy lại không cài toàn bộ từ đầu.
- Cell cuối sẽ kết thúc sau khi server sẵn sàng; tiến trình API và Cloudflare Tunnel vẫn chạy nền. Dòng `NEKO_SERVER_READY` mới là tín hiệu extension bắt đầu xử lý video.

## Kiến trúc và bảo mật

- `extension/`: Manifest V3 side panel, Douyin/Colab content scripts, canvas editor và storage cục bộ.
- `backend/`: FastAPI và pipeline xử lý trong Colab.
- `OmniVoice_API.ipynb`: launcher GPU + Cloudflare Quick Tunnel.
- Mỗi runtime sinh bearer token mới; API không chấp nhận request thiếu token.
- Cookie chỉ được ghi vào file tạm để yt-dlp đọc và bị xóa ngay sau lần tải.
- Link tải kết quả chỉ dùng một lần và hết hạn sau 10 phút.
- Quick Tunnel dành cho phiên xử lý tạm thời, không phải dịch vụ production có SLA.

## Kiểm tra phát triển

```powershell
py -3.12 -m unittest discover -s tests -v
node --check extension/service-worker.js
node --check extension/sidepanel.js
node --check extension/content/douyin.js
node --check extension/content/colab.js
node tests/test_extension.js
```

Chỉ tải, chỉnh sửa và clone giọng đối với nội dung bạn sở hữu hoặc được chủ sở hữu cho phép.
