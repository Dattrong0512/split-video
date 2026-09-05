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

Pipeline dùng Whisper tự nhận diện ngôn ngữ và lời nói rồi để Gemini sửa transcript, sửa từ đồng âm theo ngữ cảnh và gộp các mảnh Whisper liền kề thành câu hoàn chỉnh. Bản dịch giữ đủ nội dung, kể cả một cue Whisper dài hoặc chứa nhiều câu. Python chia bản dịch thành các trang phụ đề tối đa 56 ký tự, mỗi sự kiện tối đa 4,8 giây; mốc đổi trang được ước lượng trong thời lượng giọng đã căn, không dùng để chia hoặc cắt giọng TTS. Gemini vẫn phải dùng mỗi source id đúng một lần và đúng thứ tự, không gộp quá 4,8 giây hoặc qua khoảng nghỉ dài. Sau hai kết quả sai, lần sửa cuối yêu cầu dịch riêng từng source cue. Python dựng lại timestamp lời thoại từ các source cue; OCR chỉ phục vụ tìm vùng blur. Với một giọng, Gemini không nhận trường speaker/gender và mọi câu bị khóa vào cùng vai `S1`. Với 2–4 giọng, Gemini phải dùng đúng số vai đã chọn và giữ cùng mã cho cùng nhân vật/góc nhìn. Render từ chối cấu hình thừa, thiếu hoặc trùng giọng. Tốc độ được căn theo thời lượng từng câu gốc, giới hạn tự động 0,75–1,08× trước khi áp dụng thanh tốc độ chung. Các mảnh lời ngắn liền kề của cùng nhân vật được gộp trước khi tạo giọng; khoảng im lặng thừa ở đầu/cuối audio được bỏ. Pipeline mượn tối đa 0,5 giây khoảng nghỉ. Nếu vẫn thiếu thời gian, chế độ mặc định **Giữ giọng tự nhiên · kéo dài đoạn hình** làm chậm đoạn hình cùng nhạc nền tương ứng và dời các câu sau theo cùng mốc; video có thể dài hơn bản gốc. Chế độ **Giữ thời lượng video · tăng tốc giọng** giữ mốc hình gốc và tăng tốc audio đủ để câu đọc vừa, có thể vượt giới hạn tốc độ tự động thông thường. Preview dùng cùng cách căn và cùng audio đã tạo với bản xuất, kể cả câu đi qua mốc 30 giây. Câu dài được xử lý bằng chế độ đã chọn, không buộc đổi giọng hoặc âm thầm cắt mất lời. Khi đổi chế độ, cần tạo lại preview trước khi xuất toàn bộ. Audio xuất chỉ giữ `no_vocals` từ Demucs cùng giọng Việt; vocal gốc không còn được trộn lại. Nếu Demucs thất bại, pipeline dùng nền im lặng thay vì đưa giọng gốc trở lại.

Với voice clone, OmniVoice được tạo ở 32 bước. Chế độ kéo dài hình cho giọng đọc tự nhiên ở mỗi lần tạo. Chế độ giữ thời lượng video thử giọng tự nhiên trước; nếu quá ngắn hoặc quá dài, các lần sau yêu cầu thời lượng câu gốc. Whisper kiểm tra transcript của toàn bộ clip tạo ra, gồm cả phần kết câu; clip thiếu phần cuối không được chấp nhận chỉ nhờ điểm khớp một phần. Generation được thử tối đa ba lần bằng chính giọng đã chọn; nếu vẫn không khớp câu đích, job dừng với lỗi thay vì âm thầm đổi sang Hoài My. Nếu chỉ còn bản đọc đủ nhưng quá dài, pipeline giữ bản đó và tự căn theo chế độ đã chọn. Cache TTS cũ sẽ tự bị loại khi thuật toán tạo giọng thay đổi.

Mỗi link preview gắn với đúng bản âm thanh đã tạo, nên mở thêm tab hoặc tạo preview mới không làm thay đổi nội dung của link cũ. Lỗi tải preview có thời gian chờ tối đa 30 giây và cho phép thử lại.

### Áp dụng bản sửa 1.5.10

Notebook hiện lấy backend từ nhánh `main` của `Dattrong0512/split-video` trên GitHub. Thay đổi trong thư mục trên máy chưa tự cập nhật lên Colab: cần đưa bản sửa lên kho mã đó trước, chạy lại notebook và nạp lại extension. Nên nghe thử ở **1,00×** trước khi chỉnh thanh tốc độ. Kiểm thử cục bộ dùng audio tổng hợp; chất lượng và nhịp đọc của giọng clone cụ thể cần được nghe kiểm tra trên Colab.

Bản 1.5.10 không còn hủy render chỉ vì Whisper chấm thấp giọng clone. Pipeline vẫn thử tối đa ba lần, cắt đuôi lặp nếu phát hiện được và dùng Whisper để chọn bản tốt nhất; nếu Whisper nhận sai hoặc không nhận ra giọng đã tạo, bản audio hợp lệ vẫn được dùng. Chỉ lỗi tạo audio thật sự mới dừng job. Bản này cũng đổi cache TTS để không tái sử dụng kết quả theo thuật toán cũ.

Bản 1.5.9 xử lý lỗi câu đọc cần 1,52 giây nhưng chỉ có 0,40 giây trong timeline, áp dụng cho cả giọng có sẵn và giọng clone. Bạn có thể chọn kéo dài hình để giữ giọng tự nhiên hoặc tăng tốc giọng để giữ thời lượng video.

Bản 1.5.8 sửa vòng lặp lỗi định dạng do ép cả bản dịch vào giới hạn của một trang phụ đề, đồng thời hiển thị lý do kiểm tra thất bại nếu Gemini vẫn trả dữ liệu thiếu hoặc sai. Extension hiện chỉ hỗ trợ Douyin: bấm phân tích trên tab TikTok hoặc trang khác sẽ yêu cầu mở Douyin, không tự lấy video cũ từ tab khác. Khi tự tiếp tục sau lúc chờ Colab, extension dùng tab Douyin đã ghi nhớ và dừng nếu video trong tab đó đã đổi.

### Douyin phát được nhưng Colab không tải được

Phát video trong Chrome và tải bằng yt-dlp trên Colab là hai yêu cầu khác nhau. Douyin có thể không trả API chi tiết video cho Colab dù Chrome vẫn xem bình thường; lỗi `Fresh cookies` của yt-dlp không đủ để kết luận phiên đăng nhập đã hết hạn.

Bản 1.5.7 lấy lại đúng video trước mỗi lần phân tích, ghi nhớ tab Douyin khi chuyển sang Colab và gửi User-Agent của Chrome cùng link media hiện tại. Với player dùng `blob:`, extension tìm link trong dữ liệu trang theo đúng ID video. Colab ưu tiên tải media trực tiếp, kiểm tra đủ cả hình lẫn âm thanh rồi mới dùng yt-dlp dự phòng. Link media chỉ được gửi đến phiên Colab và không xuất hiện trong API trạng thái job. Nếu cả hai cách đều bị từ chối, giao diện báo lỗi truy cập từ Colab và đề nghị phát video rồi thử lại, không khẳng định cookie hết hạn. Cách lấy dữ liệu player phụ thuộc cấu trúc Douyin; cần kiểm tra trên trang thật nếu Douyin thay đổi player hoặc ràng buộc link theo IP.

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
