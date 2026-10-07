# Gán 68 nghề DESMAP với mã O\*NET-SOC

> **Trạng thái: đã duyệt ngày 06/10/2026.** Bản máy đọc: `data/onet-career-mapping.json`. Dữ liệu O\*NET® 30.2 được trích bởi `scripts/build_onet_extract.py` vào `data/onet-extract.json`, sau đó `scripts/build_career_catalog.py` tạo danh mục nghề.

Mức tin cậy: **H** = tương đương trực tiếp (38 nghề) · **M** = ghép hoặc gần đúng (23) · **L** = O\*NET không có mã tương đương, dùng mã ghép (7). Khi một nghề có nhiều mã, hồ sơ là trung bình của các mã.

Đã bỏ 4 nghề vì dữ liệu O\*NET của Mỹ khó đại diện cho bối cảnh Việt Nam: Công chứng viên, Chuyên viên hành chính công, Chuyên viên quan hệ quốc tế, Nhà nghiên cứu. Có thể thêm lại khi có trọng số do chuyên gia chấm.

## Kinh doanh, khởi nghiệp

| Nghề | Mã O\*NET-SOC | Mức | Ghi chú |
|---|---|---|---|
| Nhà khởi nghiệp (`nha-khoi-nghiep`) | `11-1011.00` Chief Executives<br>`11-1021.00` General and Operations Managers | L | O*NET không có mã khởi nghiệp; dùng CEO + quản lý chung |
| Chuyên viên phát triển kinh doanh (`chuyen-vien-phat-trien-kinh-doanh`) | `13-1161.00` Market Research Analysts and Marketing Specialists<br>`11-2022.00` Sales Managers | L | Không có mã BD riêng; ghép nghiên cứu thị trường + quản lý bán hàng |
| Quản lý sản phẩm (`quan-ly-san-pham`) | `11-2021.00` Marketing Managers<br>`15-1299.09` Information Technology Project Managers | L | Không có Product Manager; ghép quản lý marketing + quản lý dự án CNTT |
| Chuyên viên marketing (`chuyen-vien-marketing`) | `13-1161.00` Market Research Analysts and Marketing Specialists<br>`11-2021.00` Marketing Managers | M | Ghép Market Research Analysts and Marketing Specialists + Marketing Managers để tách khỏi nghề Nghiên cứu thị trường |
| Chuyên viên tài chính (`chuyen-vien-tai-chinh`) | `13-2041.00` Credit Analysts<br>`13-2099.01` Financial Quantitative Analysts | M | Mã 13-2051.00 Financial and Investment Analysts chưa có điểm đánh giá; dùng Credit Analysts + Financial Quantitative Analysts |
| Quản lý bán hàng (`quan-ly-ban-hang`) | `11-2022.00` Sales Managers | H |  |
| Chuyên viên thương mại điện tử (`chuyen-vien-thuong-mai-dien-tu`) | `13-1161.01` Search Marketing Strategists<br>`13-1161.00` Market Research Analysts and Marketing Specialists | M | Search Marketing Strategists gần nhất |
| Nhân viên kinh doanh (`sales-representative`) | `41-4012.00` Sales Representatives, Wholesale and Manufacturing, Except Technical and Scientific Products | H |  |

## Thiết kế, sáng tạo

| Nghề | Mã O\*NET-SOC | Mức | Ghi chú |
|---|---|---|---|
| Nhà thiết kế đồ họa (`thiet-ke-do-hoa`) | `27-1024.00` Graphic Designers | H |  |
| Nhà thiết kế UX/UI (`thiet-ke-ux-ui`) | `15-1254.00` Web Developers<br>`27-1021.00` Commercial and Industrial Designers | L | Mã 15-1255.00 Web and Digital Interface Designers chưa có điểm đánh giá; ghép Web Developers + Commercial and Industrial Designers |
| Kiến trúc sư (`kien-truc-su`) | `17-1011.00` Architects, Except Landscape and Naval | H |  |
| Nhà thiết kế nội thất (`thiet-ke-noi-that`) | `27-1025.00` Interior Designers | H |  |
| Nhà thiết kế sản phẩm (`thiet-ke-san-pham`) | `27-1021.00` Commercial and Industrial Designers | H | Commercial and Industrial Designers |
| Giám đốc nghệ thuật (`dao-dien-nghe-thuat`) | `27-1011.00` Art Directors | H | Art Directors |
| Họa sĩ minh họa (`hoa-si-minh-hoa`) | `27-1013.00` Fine Artists, Including Painters, Sculptors, and Illustrators | M | Mã gộp họa sĩ, điêu khắc, minh họa |

## Môi trường, bền vững

| Nghề | Mã O\*NET-SOC | Mức | Ghi chú |
|---|---|---|---|
| Kỹ sư môi trường (`ky-su-moi-truong`) | `17-2081.00` Environmental Engineers | H |  |
| Chuyên viên phát triển bền vững (`chuyen-vien-phat-trien-ben-vung`) | `13-1199.05` Sustainability Specialists | H | Sustainability Specialists |
| Kỹ sư năng lượng tái tạo (`ky-su-nang-luong-tai-tao`) | `17-2199.11` Solar Energy Systems Engineers<br>`17-2199.10` Wind Energy Engineers | M | Trung bình kỹ sư điện mặt trời + điện gió |
| Chuyên viên bảo tồn (`chuyen-vien-bao-ton`) | `19-1031.00` Conservation Scientists | H | Conservation Scientists |
| Nhà nghiên cứu môi trường (`nghien-cuu-moi-truong`) | `19-2041.00` Environmental Scientists and Specialists, Including Health | H | Environmental Scientists and Specialists |
| Chuyên viên ESG (`chuyen-vien-esg`) | `13-1199.05` Sustainability Specialists<br>`13-1041.00` Compliance Officers | L | Chưa có mã ESG; ghép bền vững + tuân thủ |
| Chuyên viên quản lý tài nguyên (`quan-ly-tai-nguyen`) | `11-9121.02` Water Resource Specialists<br>`19-1031.00` Conservation Scientists | L | Water Resource Specialists + Conservation Scientists |

## Sức khỏe

| Nghề | Mã O\*NET-SOC | Mức | Ghi chú |
|---|---|---|---|
| Bác sĩ (`doctor`) | `29-1215.00` Family Medicine Physicians<br>`29-1216.00` General Internal Medicine Physicians | M | Bác sĩ đa khoa: gia đình + nội tổng quát |
| Điều dưỡng (`dieu-duong`) | `29-1141.00` Registered Nurses | H | Registered Nurses |
| Dược sĩ (`duoc-si`) | `29-1051.00` Pharmacists | H |  |
| Chuyên gia tâm lý (`chuyen-gia-tam-ly`) | `19-3033.00` Clinical and Counseling Psychologists | H | Clinical and Counseling Psychologists |
| Chuyên gia dinh dưỡng (`chuyen-gia-dinh-duong`) | `29-1031.00` Dietitians and Nutritionists | H |  |
| Chuyên viên y tế công cộng (`chuyen-vien-y-te-cong-cong`) | `19-1041.00` Epidemiologists<br>`21-1091.00` Health Education Specialists | M | Dịch tễ + giáo dục sức khỏe |
| Kỹ thuật viên xét nghiệm (`ky-thuat-vien-xet-nghiem`) | `29-2011.00` Medical and Clinical Laboratory Technologists | H | Medical and Clinical Laboratory Technologists |

## Luật, dịch vụ công

| Nghề | Mã O\*NET-SOC | Mức | Ghi chú |
|---|---|---|---|
| Luật sư (`lawyer`) | `23-1011.00` Lawyers | H |  |
| Chuyên viên pháp chế (`chuyen-vien-phap-che`) | `23-1011.00` Lawyers<br>`23-2011.00` Paralegals and Legal Assistants | M | Luật sư + trợ lý pháp lý; VN pháp chế doanh nghiệp |
| Chuyên viên chính sách công (`chuyen-vien-chinh-sach-cong`) | `19-3094.00` Political Scientists<br>`19-3011.00` Economists | M | Political Scientists + Economists |
| Chuyên viên tuân thủ (`chuyen-vien-tuan-thu`) | `13-1041.00` Compliance Officers | H | Compliance Officers |

## Truyền thông

| Nghề | Mã O\*NET-SOC | Mức | Ghi chú |
|---|---|---|---|
| Nhà báo (`nha-bao`) | `27-3023.00` News Analysts, Reporters, and Journalists | H |  |
| Chuyên viên quan hệ công chúng (`chuyen-vien-quan-he-cong-chung`) | `27-3031.00` Public Relations Specialists | H |  |
| Nhà sáng tạo nội dung (`nha-sang-tao-noi-dung`) | `27-3043.00` Writers and Authors<br>`27-4032.00` Film and Video Editors | L | Không có mã content creator; ghép người viết + dựng video |
| Biên tập viên (`bien-tap-vien`) | `27-3041.00` Editors | H | Editors |
| Chuyên viên truyền thông số (`chuyen-vien-truyen-thong-so`) | `13-1161.01` Search Marketing Strategists<br>`27-3031.00` Public Relations Specialists | M |  |
| Biên kịch (`bien-kich`) | `27-3043.05` Poets, Lyricists and Creative Writers | H | Poets, Lyricists and Creative Writers |
| Nhà sản xuất truyền thông (`nha-san-xuat-truyen-thong`) | `27-2012.00` Producers and Directors | H | Producers and Directors |

## Vận hành, kỹ thuật nghề

| Nghề | Mã O\*NET-SOC | Mức | Ghi chú |
|---|---|---|---|
| Chuyên viên logistics (`chuyen-vien-logistics`) | `13-1081.00` Logisticians | H | Logisticians |
| Chuyên viên chuỗi cung ứng (`chuyen-vien-chuoi-cung-ung`) | `11-3071.04` Supply Chain Managers<br>`13-1081.02` Logistics Analysts | M |  |
| Quản lý vận hành (`quan-ly-van-hanh`) | `11-1021.00` General and Operations Managers | H | General and Operations Managers |
| Kỹ thuật viên điện (`ky-thuat-vien-dien`) | `47-2111.00` Electricians | H | Electricians |
| Kỹ thuật viên cơ khí (`ky-thuat-vien-co-khi`) | `17-3027.00` Mechanical Engineering Technologists and Technicians<br>`49-9041.00` Industrial Machinery Mechanics | M | Kỹ thuật viên cơ khí + thợ máy công nghiệp |
| Chuyên viên quản lý chất lượng (`chuyen-vien-quan-ly-chat-luong`) | `11-3051.01` Quality Control Systems Managers<br>`19-4099.01` Quality Control Analysts | M |  |
| Điều phối sản xuất (`dieu-phoi-san-xuat`) | `43-5061.00` Production, Planning, and Expediting Clerks<br>`11-3051.00` Industrial Production Managers | M |  |

## Giáo dục, con người

| Nghề | Mã O\*NET-SOC | Mức | Ghi chú |
|---|---|---|---|
| Giáo viên (`teacher`) | `25-2031.00` Secondary School Teachers, Except Special and Career/Technical Education | H | Giáo viên THPT; nếu dạy cấp khác thì đổi mã |
| Chuyên viên đào tạo (`chuyen-vien-dao-tao`) | `13-1151.00` Training and Development Specialists | H | Training and Development Specialists |
| Chuyên viên nhân sự (`chuyen-vien-nhan-su`) | `13-1071.00` Human Resources Specialists | H |  |
| Chuyên viên tư vấn hướng nghiệp (`tu-van-huong-nghiep`) | `21-1012.00` Educational, Guidance, and Career Counselors and Advisors | H |  |
| Nhân viên công tác xã hội (`cong-tac-xa-hoi`) | `21-1021.00` Child, Family, and School Social Workers | M | Child, Family, and School Social Workers |
| Chuyên viên quản lý giáo dục (`quan-ly-giao-duc`) | `11-9032.00` Education Administrators, Kindergarten through Secondary<br>`25-9031.00` Instructional Coordinators | M |  |
| Giáo viên giáo dục đặc biệt (`giao-vien-giao-duc-dac-biet`) | `25-2058.00` Special Education Teachers, Secondary School | M | Cấp THPT; có thể đổi sang 25-2056/2057 |

## Khoa học, nghiên cứu

| Nghề | Mã O\*NET-SOC | Mức | Ghi chú |
|---|---|---|---|
| Chuyên viên phòng thí nghiệm (`chuyen-vien-phong-thi-nghiem`) | `19-4021.00` Biological Technicians<br>`19-4031.00` Chemical Technicians | M | Biological + Chemical Technicians |
| Chuyên viên công nghệ sinh học (`chuyen-vien-cong-nghe-sinh-hoc`) | `19-1021.00` Biochemists and Biophysicists<br>`19-1029.01` Bioinformatics Scientists | M |  |
| Nhà khoa học dữ liệu (`nha-khoa-hoc-du-lieu`) | `15-2041.00` Statisticians<br>`15-1221.00` Computer and Information Research Scientists | M | Mã 15-2051.00 Data Scientists chưa có điểm đánh giá; dùng Statisticians + Computer and Information Research Scientists |
| Chuyên viên thống kê (`chuyen-vien-thong-ke`) | `15-2041.00` Statisticians | H | Statisticians |
| Chuyên viên nghiên cứu thị trường (`nghien-cuu-thi-truong`) | `13-1161.00` Market Research Analysts and Marketing Specialists | H |  |
| Nhà nghiên cứu y sinh (`nghien-cuu-y-sinh`) | `19-1042.00` Medical Scientists, Except Epidemiologists | H | Medical Scientists, Except Epidemiologists |

## Công nghệ, kỹ thuật

| Nghề | Mã O\*NET-SOC | Mức | Ghi chú |
|---|---|---|---|
| Kỹ sư phần mềm (`ky-su-phan-mem`) | `15-1252.00` Software Developers | H | Software Developers |
| Chuyên viên phân tích dữ liệu (`chuyen-vien-phan-tich-du-lieu`) | `15-2051.01` Business Intelligence Analysts | M | Business Intelligence Analysts |
| Chuyên gia an ninh mạng (`chuyen-gia-an-ninh-mang`) | `15-1212.00` Information Security Analysts | H | Information Security Analysts |
| Kỹ sư AI (`ky-su-ai`) | `15-1221.00` Computer and Information Research Scientists | M | Không có mã AI Engineer; Data Scientists chưa có điểm đánh giá |
| Kỹ sư tự động hóa (`ky-su-tu-dong-hoa`) | `17-2199.05` Mechatronics Engineers<br>`17-2199.08` Robotics Engineers | M | Mechatronics + Robotics Engineers |
| Kỹ sư điện tử (`ky-su-dien-tu`) | `17-2072.00` Electronics Engineers, Except Computer | H | Electronics Engineers, Except Computer |
| Quản trị hệ thống (`quan-tri-he-thong`) | `15-1244.00` Network and Computer Systems Administrators | H | Network and Computer Systems Administrators |
| Kỹ sư ô tô (`automotive-engineer`) | `17-2141.02` Automotive Engineers | H | Automotive Engineers |

## Mã thay thế khi thiếu dữ liệu

| Mã gốc | File thiếu | Dùng dữ liệu của |
|---|---|---|
| `15-1252.00` Software Developers | WorkValues | `15-1299.08` |
| `13-1199.05` Sustainability Specialists | WorkStyles | `19-2041.00` |
| `17-2199.10` Wind Energy Engineers | WorkStyles | `17-2081.00` |
| `17-2199.11` Solar Energy Systems Engineers | WorkStyles | `17-2081.00` |
| `11-9121.02` Water Resource Specialists | WorkStyles | `19-1031.00` |

Ba mã tồn tại nhưng chưa có điểm đánh giá trong bản 30.2 nên đã thay bằng mã gần nhất: `13-2051.00` Financial and Investment Analysts, `15-1255.00` Web and Digital Interface Designers, `15-2051.00` Data Scientists. Khi O\*NET bổ sung dữ liệu, nên dùng lại các mã này.

## Cách gom mục O\*NET vào nhóm DESMAP

| Nhóm | Mục O\*NET |
|---|---|
| D1 Thu nhập, phúc lợi, ổn định | Work Values: Achievement (Extent, mục tiêu D) |
| D2 Học hỏi, phát triển, thử thách | Work Values: Support (Extent, mục tiêu D) |
| D3 Tự chủ | Work Values: Independence (Extent, mục tiêu D) |
| D4 Ý nghĩa và đóng góp | Work Values: Relationships (Extent, mục tiêu D) |
| D5 Công nhận và ảnh hưởng | Work Values: Recognition (Extent, mục tiêu D) |
| D6 Điều kiện và cân bằng công việc | Work Values: Working Conditions (Extent, mục tiêu D) |
| E1 Kỹ năng nền tảng | Skills: 10 kỹ năng thuộc nhóm tương ứng của O\*NET |
| E2 Giải quyết vấn đề phức tạp | Skills: 1 kỹ năng thuộc nhóm tương ứng của O\*NET |
| E3 Kỹ năng tương tác xã hội | Skills: 6 kỹ năng thuộc nhóm tương ứng của O\*NET |
| E4 Kỹ năng kỹ thuật | Skills: 11 kỹ năng thuộc nhóm tương ứng của O\*NET |
| E5 Kỹ năng hệ thống | Skills: 3 kỹ năng thuộc nhóm tương ứng của O\*NET |
| E6 Quản lý nguồn lực | Skills: 4 kỹ năng thuộc nhóm tương ứng của O\*NET |
| S1 Vai trò hướng nhiệm vụ | WorkActivities: Developing Objectives and Strategies; WorkActivities: Interpreting the Meaning of Information for Others; WorkActivities: Communicating with Supervisors, Peers, or Subordinates |
| S2 Vai trò duy trì quan hệ | WorkActivities: Establishing and Maintaining Interpersonal Relationships; WorkActivities: Developing and Building Teams; WorkStyles: Cooperation |
| S3 Vai trò định hướng cá nhân | WorkStyles: Leadership Orientation; WorkActivities: Selling or Influencing Others; WorkContext: Freedom to Make Decisions |
| M1 Tư duy phân tích | WorkActivities: Analyzing Data or Information; WorkActivities: Processing Information |
| M2 Tư duy sáng tạo | WorkActivities: Thinking Creatively; WorkStyles: Innovation |
| M3 Tư duy thực tiễn | WorkActivities: Making Decisions and Solving Problems; WorkStyles: Adaptability |
| A1 Chuẩn bị cho tương lai | WorkActivities: Updating and Using Relevant Knowledge; WorkStyles: Achievement Orientation |
| A2 Chủ động và chịu trách nhiệm | WorkStyles: Initiative; WorkStyles: Dependability |
| A3 Khám phá khả năng mới | WorkStyles: Intellectual Curiosity; WorkStyles: Tolerance for Ambiguity |
| A4 Tự tin vượt qua khó khăn | WorkStyles: Perseverance; WorkStyles: Self-Confidence |
| P1 Áp lực thời gian và tốc độ | WorkContext: Time Pressure |
| P2 Áp lực khối lượng công việc | WorkActivities: Organizing, Planning, and Prioritizing Work; WorkActivities: Scheduling Work and Activities |
| P3 Áp lực tư duy và quyết định | WorkContext: Frequency of Decision Making; WorkContext: Impact of Decisions on Co-workers or Company Results |
| P4 Áp lực cảm xúc | WorkStyles: Stress Tolerance; WorkStyles: Self-Control |
| P5 Áp lực tương tác và xung đột | WorkContext: Conflict Situations; WorkContext: Dealing With Unpleasant, Angry, or Discourteous People |
| P6 Áp lực trách nhiệm và hậu quả | WorkContext: Consequence of Error; WorkContext: Work Outcomes and Results of Other Workers; WorkContext: Health and Safety of Other Workers |

D1 và D2 được ghép theo nội dung câu hỏi hiện tại (D1 đo mức thích thử thách, D2 đo nhu cầu được hướng dẫn), không theo tên nhóm. Nếu sửa câu hỏi hoặc tên nhóm, cần sửa `DESIRE_VALUES` trong `scripts/build_onet_extract.py`.

## Ghi nguồn bắt buộc (CC BY 4.0)

> Includes information from the O*NET 30.2 Database by the U.S. Department of Labor, Employment and Training Administration (USDOL/ETA). Used under the CC BY 4.0 license. The DESMAP team has modified all or some of this information. USDOL/ETA has not approved, endorsed, or tested these modifications.

Web hiển thị bản tiếng Việt của thông báo này dưới danh sách gợi ý nghề và ở chân trang (`OnetAttribution.svelte`).
