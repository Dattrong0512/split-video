# Split Video

Ứng dụng Windows chạy CPU để tải video bạn có quyền sử dụng hoặc mở video cục bộ, tạo phụ đề/lồng tiếng Việt rồi xuất một video hoàn chỉnh.

## Tính năng

- Dán liên kết Douyin (hoặc liên kết mà `yt-dlp` hỗ trợ) và tải video, hoặc mở video có sẵn trên máy.
- Whisper chạy CPU/int8 để nhận diện lời thoại có timecode.
- Gemini nghe audio, hiệu đính transcript, dịch tiếng Trung/Anh sang tiếng Việt và phân biệt người nói; timecode vẫn giữ từ Whisper.
- Chọn riêng giọng nữ Hoài My hoặc nam Nam Minh cho mỗi người nói, sau đó tạo lồng tiếng Việt khớp mốc từng câu.
- Kéo khung xanh lá trên ảnh xem trước để đặt phụ đề Việt; phụ đề được render trực tiếp vào video. Kéo khung hồng để làm mờ subtitle gốc trước khi render bản mới.
- Xuất video hoàn chỉnh từ đầu đến cuối, không chia đoạn.
- Kéo chuột khoanh vùng watermark để làm mờ.
- Thêm nhiều vùng làm mờ, nhiều watermark chữ và watermark ảnh có thể đổi kích thước.
- Kéo các cạnh/góc khung crop ngay trên ảnh xem trước; dùng thanh Zoom để phóng to khung hình trước khi crop.
- Render bằng CPU với FFmpeg. Không cần GPU.

## Tạo file EXE

1. Cài Python 3.11+ và đánh dấu **Add Python to PATH** khi cài.
2. Double-click `build_exe.bat`.
3. File dùng cho người khác nằm tại `release\Split Video - Ready.exe`.

Đây là bản một-file. Bạn chỉ cần gửi đúng file `.exe` trong thư mục `release`.

## Quy trình phụ đề & lồng tiếng

1. Dán liên kết Douyin, bấm **Tải video**, hoặc mở video cục bộ.
2. Nhập khóa Gemini vào ô trong ứng dụng. Khóa chỉ nằm trong RAM; có thể đặt `GEMINI_API_KEY` trong Windows nếu không muốn nhập lại.
3. Bấm **1. Nhận diện, dịch & phân vai**. Lần đầu Whisper tải model nên cần mạng và mất thêm thời gian.
4. Chọn giọng nữ/nam cho từng nhãn người nói, khoanh vùng subtitle gốc bằng khung hồng và đặt khung xanh lá ở chỗ muốn hiện phụ đề Việt.
5. Bấm **Xuất video**. Nếu bật lồng tiếng Việt, quá trình tạo giọng dùng dịch vụ Edge TTS; phần nhận diện và render video chạy CPU, không dùng GPU.

Gemini nhận audio 16 kHz đã nén để sửa/biên dịch/phân vai, nên không dùng khóa API trong mã nguồn hay lưu khóa vào file.

## Lưu ý bản quyền

Chỉ tải và xử lý video mà bạn sở hữu hoặc được chủ sở hữu cho phép tải/chỉnh sửa. Việc xử lý watermark chỉ nên dùng cho nội dung mà bạn có quyền chỉnh sửa.
