# Douyin Vietnamese Dubbing

Chrome extension lấy video Douyin đang hiển thị, tạo phụ đề và lồng tiếng Việt trên Google Colab, làm mờ subtitle/watermark gốc rồi tự tải MP4 hoàn chỉnh.

## Cài extension

1. Mở `chrome://extensions` và bật **Developer mode**.
2. Chọn **Load unpacked** rồi chọn thư mục `extension` của dự án.
3. Ghim biểu tượng **Douyin Vietnamese Dubbing** lên thanh công cụ.

## Thiết lập một lần

1. Tạo **Gemini API key mới**. Không dùng lại key đã từng gửi công khai.
2. Mở extension, dán key và bấm **Lưu**. Key chỉ nằm trong Chrome profile hiện tại, không được ghi vào source hoặc đồng bộ.
3. Đăng nhập Douyin trong cùng Chrome profile. Extension tự đọc và cập nhật cookie hiện tại khi mở panel và ngay trước mỗi lần phân tích; `cookies.txt` chỉ còn là lựa chọn dự phòng.
4. Voice clone là clip một người nói rõ, dài 3–10 giây. Chỉ clone giọng bạn có quyền sử dụng.

## Sử dụng

1. Mở video trên `douyin.com`; có thể là trang feed/discover hoặc URL `/video/<id>`.
2. Bấm biểu tượng extension để mở side panel. Extension tự lấy URL chuẩn của video đang phát.
3. Chọn số giọng nhân vật. Mặc định **1** dùng cùng một preset/voice clone cho toàn bộ lời thoại và Gemini chỉ sửa transcript/dịch phụ đề, không phân vai. Nếu video có nhiều nhân vật, chọn **2**, **3** hoặc **4** rồi gán đúng số giọng khác nhau cho từng vai trước khi bấm phân tích.
4. Extension tự tạo bản xem trước 30 giây và tự dừng video Douyin gốc để âm thanh không phát chồng. Bấm **Mở preview lớn trong tab riêng** nếu cần xem toàn khung hình; player nhỏ sẽ dừng và tab lớn tiếp tục đúng câu/thời điểm hiện tại. Tab lớn vẫn có thanh tốc độ và nút xuất. Kéo tốc độ giọng trong khoảng 0,80–1,40× để nghe phản hồi tức thì; khi thả thanh, extension tự tạo lại preview chính xác rồi tiếp tục ở vị trí đang nghe. Khi đã khớp giọng gốc, bấm xuất toàn bộ video.
5. Bạn có thể bấm **Run all** trong `OmniVoice_API.ipynb` trước rồi mới bấm **Phân tích video**. Extension sẽ gắn vào đúng tab Colab đang mở và dùng server đã chạy; nó không refresh notebook hoặc mở thêm tab. Nếu chưa có notebook, extension mới tự mở một tab, kết nối GPU T4 và chạy notebook. Giữ tab Colab mở cho tới khi tải bắt đầu.

Pipeline dùng Whisper nhận dạng lời nói rồi để Gemini sửa transcript, sửa từ đồng âm theo ngữ cảnh và gộp các mảnh Whisper liền kề thành câu hoàn chỉnh. Mỗi subtitle chỉ được chứa một câu, tối đa 4,8 giây và tối đa 56 ký tự để không biến thành đoạn văn nhiều dòng; output vi phạm sẽ được yêu cầu tạo lại. Python dựng lại timestamp từ chính các source cue nên Gemini không thể tự bịa thời gian; OCR chỉ phục vụ tìm vùng blur. Với một giọng, Gemini không nhận trường speaker/gender và mọi câu bị khóa vào cùng vai `S1`. Với 2–4 giọng, Gemini phải dùng đúng số vai đã chọn và giữ cùng mã cho cùng nhân vật/góc nhìn. Render từ chối cấu hình thừa, thiếu hoặc trùng giọng. Mỗi nhân vật dùng một tốc độ tự động thống nhất trong toàn video, giới hạn 0,95–1,08× trước khi áp dụng thanh tốc độ chung. Audio xuất chỉ giữ `no_vocals` từ Demucs cùng giọng Việt; vocal gốc không còn được trộn lại. Nếu Demucs thất bại, pipeline dùng nền im lặng thay vì đưa giọng gốc trở lại.

Với voice clone, OmniVoice được tạo ở 32 bước và được phép đọc xong toàn bộ câu trước khi pipeline căn timeline, tránh cắt mất cuối câu chỉ để ép vào một mảnh Whisper ngắn. Whisper kiểm tra transcript của toàn bộ clip tạo ra, gồm cả phần kết câu; clip thiếu phần cuối không còn được chấp nhận chỉ nhờ điểm khớp một phần. Generation sai được thử lại tối đa ba lần bằng chính giọng đã chọn; nếu vẫn không khớp câu đích, job dừng với lỗi thay vì âm thầm đổi sang Hoài My. Cache TTS cũ sẽ tự bị loại khi thuật toán tạo giọng thay đổi.

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
