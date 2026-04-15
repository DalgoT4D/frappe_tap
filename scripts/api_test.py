import tap_lms.imgana.submission as m
a = m.get_assignment_context(assignment_id="SC_L4_CA1-Basic")
print(a)


import tap_lms.imgana.submission as m
a = m.get_student_details(name="Test_Hindi", glific_id="1234")
print(a)


assignment_id="SC_L4_CA1-Basic"
assignment = frappe.get_doc("Assignment", assignment_id)
print(assignment.as_dict())


import tap_lms.feedback_handler.audio_creation as ac
text_1 = "तुमची चित्रकला खूप रंगीत आणि छान आहे! उत्तम काम करत राहा!"
text_2 = "ਤੁਹਾਡੀ ਕਲਾ ਰਚਨਾ ਸ਼ਾਨਦਾਰ ਰੰਗਾਂ ਨਾਲ ਹੈ। ਚੰਗਾ ਕੰਮ ਜਾਰੀ ਰੱਖੋ!"
text_3 = "ನಿಮ್ಮ ಚಿತ್ರದಲ್ಲಿ ಹೊಳೆಯುವ ಬಣ್ಣಗಳು ಮತ್ತು ಪುನರಾವೃತ್ತಿಯು ಗಮನ ಸೆಳೆಯುತ್ತವೆ. ಇನ್ನಷ್ಟು ವಿನ್ಯಾಸಗಳನ್ನು ಸೇರಿಸಿ!"
text_4 = "Your Pop Art project effectively uses bright colors. Try exploring more patterns for added interest"
language_name = "Marathi"
submission_id = "12345"
audio_url = ac.generate_feedback_audio(
    text=text_1,
    language_name=language_name,
    submission_id=submission_id
)
print(f"Generated audio URL: {audio_url}")


import tap_lms.imgana.submission as m
api_key = "33qwqWwre12@321"
assign_id = "VA_L1_CA1-Basic"
name1 = "Test_Hindi"
glific_id = "1234"
img_url = "https://storage.googleapis.com/bucket_tap_1/uploads/11/AugProccess/20251105103501_C155227_F32580_M18105608.mp4"
a = m.submit_artwork(api_key, assign_id, name1, glific_id, img_url)
print(a)
